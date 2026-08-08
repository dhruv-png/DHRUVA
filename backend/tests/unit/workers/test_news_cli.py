"""``dhruva-news`` wires the pass together, and refuses what it will not run.

What is asserted here is *wiring and refusal*: that the parser exists and is
shaped the way a runbook expects, that a bad account or an unparseable cutoff is
turned into a message and a status rather than a traceback, that an invalid
configuration stops composition before anything is contacted, and that the
verdict an outcome produces is the exit status a runbook step reads.

Nothing here opens a socket or a connection. The one command that would --
``poll`` -- is exercised through ``--dry-run`` and through injected feeds, which
is the whole reason polling takes its feeds rather than building them.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.intelligence.application.news_polling import BatchOutcome
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.search import plan_search_phrases
from dhruva.contexts.intelligence.domain.sources import SourceHealth, SourceStatus
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import GdeltQuery
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import ConfigurationError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import news as cli

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
OBSERVED: Final = datetime(2026, 8, 6, 14, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


def _status(health: SourceHealth) -> SourceStatus:
    return SourceStatus(health=health, reason=f"reported {health}", observed_at=OBSERVED)


def _outcome(index: int, health: SourceHealth) -> BatchOutcome:
    return BatchOutcome(index=index, source_key="gdelt", status=_status(health), items_offered=0)


# --------------------------------------------------------------------------- #
# The command surface a runbook depends on
# --------------------------------------------------------------------------- #


def test_both_subcommands_exist() -> None:
    """A poll that writes, and a read that does not."""
    parser = cli.build_parser()

    assert parser.parse_args(["poll", "--account", str(ACCOUNT)]).command == "poll"
    assert (
        parser.parse_args(["show", "--account", str(ACCOUNT), "--symbol", "SBIN"]).command == "show"
    )


def test_a_subcommand_is_required() -> None:
    """``dhruva-news`` alone must not silently do something."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_the_read_command_takes_a_cutoff_a_window_and_a_bound() -> None:
    """Point-in-time reading is not optional decoration on the command."""
    args = cli.build_parser().parse_args(
        [
            "show",
            "--account",
            str(ACCOUNT),
            "--symbol",
            "HAL",
            "--as-of",
            "2026-08-06T14:20:00+00:00",
            "--days",
            "3",
            "--limit",
            "10",
        ]
    )

    assert (args.symbol, args.days, args.limit) == ("HAL", 3, 10)
    assert args.as_of == "2026-08-06T14:20:00+00:00"


def test_the_poll_command_can_be_rehearsed_without_contacting_anything() -> None:
    """An operator should be able to read the requests before issuing them."""
    args = cli.build_parser().parse_args(["poll", "--account", str(ACCOUNT), "--dry-run"])

    assert args.dry_run is True


def test_the_help_text_says_nse_filings_are_unavailable() -> None:
    """Somebody reading ``--help`` is deciding whether this answers their question."""
    assert "NSE" in (cli.build_parser().epilog or "")


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_an_unparseable_account_is_refused_with_a_message() -> None:
    """An operator gets told what is wrong, not shown a stack trace."""
    with pytest.raises(ValidationError, match="account identifier"):
        cli._account("not-an-account")


def test_a_valid_account_parses_in_either_written_form() -> None:
    """Prefixed and bare UUIDs both appear in real runbooks."""
    assert cli._account(str(ACCOUNT)) == ACCOUNT
    assert cli._account(str(ACCOUNT.value)) == ACCOUNT


def test_an_unparseable_cutoff_is_refused() -> None:
    """A silently defaulted cutoff would answer a different question."""
    with pytest.raises(ValidationError, match="ISO-8601"):
        cli._instant("yesterday", field="--as-of")


def test_an_explicit_cutoff_is_read_as_given() -> None:
    """The instant an operator typed is the instant the archive is read at."""
    assert cli._instant("2026-08-06T14:20:00+00:00", field="--as-of") == OBSERVED


def test_a_cutoff_without_a_zone_is_read_as_utc() -> None:
    """Every stored instant is UTC (ADR-006); reading must not differ."""
    assert cli._instant("2026-08-06T14:20:00", field="--as-of") == OBSERVED


def test_a_cutoff_in_another_zone_is_converted_rather_than_truncated() -> None:
    """19:50 in Kolkata is 14:20 UTC, and the archive only knows the second."""
    assert cli._instant("2026-08-06T19:50:00+05:30", field="--as-of") == OBSERVED


def test_an_omitted_cutoff_means_now() -> None:
    """What do we know right now" is the common case."""
    before = datetime.now(UTC)

    resolved = cli._instant(None, field="--as-of")

    assert before <= resolved <= datetime.now(UTC) + timedelta(seconds=5)
    assert resolved.tzinfo is not None


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def test_invalid_configuration_stops_the_command_before_anything_is_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast at composition, not at the first request (ADR-031)."""
    monkeypatch.setenv("DHRUVA_NEWS__TIMESPAN", "1 fortnight")

    with pytest.raises(ConfigurationError):
        load_settings()


def test_the_configured_bounds_reach_the_query_that_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configuration that never reaches a request is configuration in name only."""
    monkeypatch.setenv("DHRUVA_NEWS__TIMESPAN", "12h")
    monkeypatch.setenv("DHRUVA_NEWS__MAX_RECORDS", "17")
    monkeypatch.setenv("DHRUVA_NEWS__QUERY_OVERRIDE", '"State Bank of India"')
    settings = load_settings()

    query = cli._override_query(settings.news)

    assert query.query == '"State Bank of India"'
    assert query.timespan == "12h"
    assert query.max_records == 17


def test_an_override_produces_exactly_one_query_and_no_watchlist_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override mode is exclusive, and it is exclusive structurally.

    There is no watchlist plan to accidentally issue alongside it, which is a
    stronger guarantee than a flag that something remembers to check.
    """
    monkeypatch.setenv("DHRUVA_NEWS__QUERY_OVERRIDE", '"Adani Ports"')
    news = load_settings().news

    assert news.watchlist_queries_enabled is False
    assert cli._override_query(news).query == '"Adani Ports"'


def test_one_feed_is_built_per_planned_query_in_order() -> None:
    """Request count is query count; nothing is merged or dropped in the wiring."""

    class _Client:
        """Stands in for the HTTP client; never used, because nothing is polled."""

    queries = (
        GdeltQuery(query='"Adani Power"', timespan="1d", max_records=5),
        GdeltQuery(query='"Canara Bank"', timespan="1d", max_records=5),
    )

    feeds = cli._feeds(
        _Client(),  # type: ignore[arg-type] # nothing is polled
        queries,
        min_interval_seconds=0.0,
    )

    assert len(feeds) == len(queries)


def test_every_built_feed_shares_one_pacing_gate() -> None:
    """A fresh gate per feed would only ever pace a batch against its own retries."""

    class _Client:
        """Stands in for the HTTP client; never used, because nothing is polled."""

    queries = (
        GdeltQuery(query='"Adani Power"', timespan="1d", max_records=5),
        GdeltQuery(query='"Canara Bank"', timespan="1d", max_records=5),
    )

    feeds = cli._feeds(
        _Client(),  # type: ignore[arg-type] # nothing is polled
        queries,
        min_interval_seconds=1.0,
    )

    assert feeds[0]._timing is feeds[1]._timing


def test_planned_queries_follow_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan decides what is asked; configuration decides how much."""
    monkeypatch.setenv("DHRUVA_NEWS__BATCH_SIZE", "1")
    news = load_settings().news
    plan = plan_search_phrases(
        (
            LinkableInstrument(
                instrument_id=InstrumentId.deterministic("reference", "CANBK"),
                canonical_symbol="CANBK",
                company_name="Canara Bank",
            ),
        ),
        on=OBSERVED.date(),
        batch_size=news.batch_size,
    )

    queries = cli._planned_queries(plan, news)

    assert [query.query for query in queries] == ['"Canara Bank"']
    assert queries[0].timespan == news.timespan


# --------------------------------------------------------------------------- #
# Exit status
# --------------------------------------------------------------------------- #


def test_a_clean_pass_exits_zero() -> None:
    """A runbook step needs success to be distinguishable."""
    assert cli._poll_verdict((), rate_limited=False) == 0


def test_a_rate_limited_pass_exits_non_zero() -> None:
    """Stopping early is not success, even though what arrived was kept."""
    assert cli._poll_verdict((), rate_limited=True) != 0


def test_an_unhealthy_batch_exits_non_zero() -> None:
    """Some requests failed" must not look like "there was no news"."""
    assert (
        cli._poll_verdict((_outcome(1, SourceHealth.MALFORMED_PAYLOAD),), rate_limited=False) != 0
    )


def test_the_verdict_prints_the_required_attribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """GDELT's terms attach a citation to using the data at all."""
    cli._poll_verdict((), rate_limited=False)

    assert "GDELT" in capsys.readouterr().out


def test_the_verdict_states_that_nse_filings_are_not_an_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A quiet pass must not be read as "nothing was announced"."""
    cli._poll_verdict((), rate_limited=False)

    assert "NSE" in capsys.readouterr().out


def test_a_rate_limit_tells_the_operator_not_to_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The correct response to being throttled is to wait, not to retry harder."""
    cli._poll_verdict((), rate_limited=True)

    assert "do not run it in a loop" in capsys.readouterr().err
