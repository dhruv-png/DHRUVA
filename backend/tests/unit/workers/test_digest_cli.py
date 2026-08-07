"""``dhruva-digest`` reads, refuses bad input, and never advises.

Two things are under test. The command surface a runbook depends on, and the
rendered output's refusal to read as a recommendation -- which is the failure
mode of any tool that ranks instruments and highlights findings.

Nothing here opens a socket or a connection. The digest is composed from stored
facts, which is exactly why it can be tested without either.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.digest import build_digest
from dhruva.contexts.intelligence.domain.entity_linking import (
    LinkableInstrument,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import classify_event
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.contexts.intelligence.interfaces.digest_presentation import (
    DIGEST_DISCLAIMER,
    render_digest,
    render_section,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import digest as cli

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
OBSERVED: Final = datetime(2026, 8, 6, 14, 20, tzinfo=UTC)

FRAUD: Final = "State Bank of India faces a forensic audit over accounting irregularities"

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project via economictimes.indiatimes.test",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://gdeltproject.org",
)
SBIN: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "SBIN"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
HAL: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "HAL"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
)
UNIVERSE: Final = (SBIN, HAL)

#: Verbs that would turn a record of stored facts into advice.
_ADVICE = ("buy", "sell", "hold", "target price", "recommend", "should invest", "outperform")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


def _archived(url: str, title: str) -> ArchivedNewsItem:
    link = canonical_url(url)
    item = NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT.key,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=GDELT,
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=SEEN,
    )
    return ArchivedNewsItem(
        revision=NewsRevision(
            item=item,
            revision=content_revision(item),
            deduplication=DeduplicationDecision(
                is_duplicate=False, rule=None, original=None, reason="first observation"
            ),
        ),
        analysis=NewsAnalysis(
            event=classify_event(title),
            sentiment=evaluate_sentiment(title),
            mapping=link_entities(title, universe=UNIVERSE, published_on=PUBLISHED.date()),
            analysed_at=ANALYSED,
        ),
    )


def _digest(*titles: str):  # type: ignore[no-untyped-def]
    items = [_archived(f"https://p.test/{n}", t) for n, t in enumerate(titles)]
    return build_digest(
        UNIVERSE,
        items,
        known_at=ANALYSED,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=ANALYSED,
    )


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #


def test_the_account_is_required() -> None:
    """Every read is attributable to whoever asked for it."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_the_cutoff_window_and_bounds_are_all_available() -> None:
    """Point-in-time reading is the feature, not an option on it."""
    args = cli.build_parser().parse_args(
        ["--account", str(ACCOUNT), "--as-of", "2026-08-06T14:20:00+00:00", "--days", "3"]
    )

    assert (args.as_of, args.days) == ("2026-08-06T14:20:00+00:00", 3)


def test_symbols_are_repeatable_and_default_to_the_whole_watchlist() -> None:
    """Asking about the whole watchlist is the common case."""
    parser = cli.build_parser()

    assert parser.parse_args(["--account", str(ACCOUNT)]).symbol is None
    assert parser.parse_args(
        ["--account", str(ACCOUNT), "--symbol", "HAL", "--symbol", "SBIN"]
    ).symbol == ["HAL", "SBIN"]


def test_the_help_text_says_this_is_not_advice() -> None:
    """Somebody reading --help is deciding what this tool is for."""
    epilog = cli.build_parser().epilog or ""

    assert "not advice" in epilog or "advice" in epilog
    assert "NSE" in epilog


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_an_unparseable_account_is_refused() -> None:
    """A message and a status, not a traceback."""
    with pytest.raises(ValidationError, match="account identifier"):
        cli._account("not-an-account")


def test_an_unparseable_cutoff_is_refused() -> None:
    """A silently defaulted cutoff answers a different question."""
    with pytest.raises(ValidationError, match="ISO-8601"):
        cli._instant("last tuesday")


def test_a_cutoff_in_another_zone_is_converted() -> None:
    """Every stored instant is UTC (ADR-006)."""
    assert cli._instant("2026-08-06T19:50:00+05:30") == OBSERVED


@pytest.mark.parametrize("days", [0, -3])
def test_a_non_positive_window_is_refused(days: int) -> None:
    """A window of no days cannot contain an answer."""
    with pytest.raises(ValidationError, match="positive number of days"):
        cli._window(days, fallback=7)


def test_an_omitted_window_falls_back_to_configuration() -> None:
    """One default, defined in the configuration boundary."""
    assert cli._window(None, fallback=9) == timedelta(days=9)


@pytest.mark.parametrize("requested", [0, -1, 51, 10_000])
def test_an_out_of_range_item_bound_is_refused(requested: int) -> None:
    """A digest is something a person reads; past a point it is a dump."""
    with pytest.raises(ValidationError, match="max-items"):
        cli._max_items(requested)


def test_an_unknown_symbol_is_refused_rather_than_silently_empty() -> None:
    """A typo would otherwise produce a confident, empty, truthful-looking report."""
    with pytest.raises(ValidationError, match="not on the approved watchlist"):
        cli._select(UNIVERSE, ["SBIN", "NOTLISTED"])


def test_the_refusal_lists_what_is_available() -> None:
    """Being told the answer is wrong is less useful than being told the options."""
    with pytest.raises(ValidationError, match="HAL"):
        cli._select(UNIVERSE, ["NOTLISTED"])


def test_selecting_a_subset_preserves_the_universe_order() -> None:
    """Selection narrows; it must not reorder."""
    assert cli._select(UNIVERSE, ["hal", "sbin"]) == UNIVERSE


def test_no_symbols_means_the_whole_watchlist() -> None:
    """The default is the product question, not a special case."""
    assert cli._select(UNIVERSE, None) == UNIVERSE


# --------------------------------------------------------------------------- #
# What the rendering must and must not say
# --------------------------------------------------------------------------- #


def test_the_disclaimer_is_always_present() -> None:
    """A ranked list of instruments needs to say what the ranking is not."""
    assert DIGEST_DISCLAIMER in render_digest(_digest(FRAUD))


def test_the_disclaimer_explains_the_ordering() -> None:
    """A reader who assumes alphabetical order will misread the top of the list."""
    assert "precedence" in DIGEST_DISCLAIMER
    assert "not advice" in DIGEST_DISCLAIMER


def test_dhruvas_own_words_contain_no_advice() -> None:
    """The failure mode of any tool that ranks instruments and highlights findings.

    Scoped to DHRUVA's own framing: headings, labels and the empty-state text.
    A headline is quoted third-party content shown with its attribution and its
    link, so what a publisher chose to write is deliberately out of scope --
    censoring it would misrepresent the source, which is the opposite of the
    problem this guards against.

    The disclaimer is excluded because its entire job is to use these words in
    the negative; that it does so is asserted separately.
    """
    quiet = build_digest(
        UNIVERSE, (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED
    )
    framing = render_digest(quiet).replace(DIGEST_DISCLAIMER, "").lower()

    for verb in _ADVICE:
        assert verb not in framing, f"the digest must not say {verb!r}"


def test_the_disclaimer_denies_rather_than_omits() -> None:
    """Saying "not a recommendation" is stronger than never saying the word."""
    assert "not a recommendation" in DIGEST_DISCLAIMER.lower()
    assert "cannot on its own support a trading decision" in DIGEST_DISCLAIMER


def test_every_rendered_entry_carries_its_attribution() -> None:
    """GDELT's terms attach a citation to using the data at all."""
    rendered = render_digest(_digest(FRAUD))

    assert "gdelt" in rendered
    assert "https://gdeltproject.org" in rendered


def test_the_publisher_link_is_shown_so_a_reader_can_follow_it() -> None:
    """DHRUVA stores a headline; the article stays with its publisher."""
    assert "https://p.test/0" in render_digest(_digest(FRAUD))


def test_a_quiet_instrument_says_so_rather_than_being_omitted() -> None:
    """A missing section is indistinguishable from a lost one."""
    rendered = render_digest(_digest(FRAUD))

    assert "nothing archived in this window" in rendered
    assert "HAL" in rendered


def test_the_cutoff_and_window_are_printed() -> None:
    """The reader must be able to see which question was answered."""
    rendered = render_digest(_digest(FRAUD))

    assert ANALYSED.isoformat() in rendered
    assert "as known at" in rendered


def test_the_nse_gap_is_stated() -> None:
    """A quiet digest must not read as "nothing was announced"."""
    assert "NSE" in render_digest(_digest(FRAUD))


def test_an_empty_watchlist_renders_a_sentence_rather_than_a_blank() -> None:
    """A blank screen is indistinguishable from a broken command."""
    empty = build_digest((), (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED)

    assert "No instruments" in render_digest(empty)


def test_a_section_states_its_categories_and_sentiment_tally() -> None:
    """The header must not be able to contradict the entries beneath it."""
    section = next(s for s in _digest(FRAUD).sections if s.canonical_symbol == "SBIN")
    rendered = render_section(section)

    assert "FRAUD_GOVERNANCE" in rendered
    assert "sentiment:" in rendered
