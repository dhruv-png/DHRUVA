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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    ATTENTION_DISCLAIMER,
    rank_watchlist,
    render_attention,
)
from dhruva.contexts.intelligence.interfaces.digest_presentation import (
    DIGEST_DISCLAIMER,
    render_digest,
    render_section,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.domain.market_context import (
    DEFAULT_MULTI_DAY_SESSIONS,
    MarketContext,
    absent_context,
    summarise_recent_bars,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import digest as cli
from dhruva.workers.cli_arguments import (
    parse_account,
    parse_cutoff,
    parse_max_items,
    parse_sessions,
    parse_window,
    select_instruments,
)

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
    """A message and a status, not a traceback.

    The input has to fail *both* accepted forms. ``not-an-account`` no longer
    does: it is a perfectly good stable label, which is the point of the label
    form and a reminder that "looks wrong to a human" is not the rule.
    """
    with pytest.raises(ValidationError, match="account identifier"):
        parse_account("Not An Account")


def test_an_unparseable_cutoff_is_refused() -> None:
    """A silently defaulted cutoff answers a different question."""
    with pytest.raises(ValidationError, match="ISO-8601"):
        parse_cutoff("last tuesday")


def test_a_cutoff_in_another_zone_is_converted() -> None:
    """Every stored instant is UTC (ADR-006)."""
    assert parse_cutoff("2026-08-06T19:50:00+05:30") == OBSERVED


@pytest.mark.parametrize("days", [0, -3])
def test_a_non_positive_window_is_refused(days: int) -> None:
    """A window of no days cannot contain an answer."""
    with pytest.raises(ValidationError, match="positive number of days"):
        parse_window(days, fallback=7)


def test_an_omitted_window_falls_back_to_configuration() -> None:
    """One default, defined in the configuration boundary."""
    assert parse_window(None, fallback=9) == timedelta(days=9)


@pytest.mark.parametrize("requested", [0, -1, 51, 10_000])
def test_an_out_of_range_item_bound_is_refused(requested: int) -> None:
    """A digest is something a person reads; past a point it is a dump."""
    with pytest.raises(ValidationError, match="max-items"):
        parse_max_items(requested)


def test_an_unknown_symbol_is_refused_rather_than_silently_empty() -> None:
    """A typo would otherwise produce a confident, empty, truthful-looking report."""
    with pytest.raises(ValidationError, match="not on the approved watchlist"):
        select_instruments(UNIVERSE, ["SBIN", "NOTLISTED"])


def test_the_refusal_lists_what_is_available() -> None:
    """Being told the answer is wrong is less useful than being told the options."""
    with pytest.raises(ValidationError, match="HAL"):
        select_instruments(UNIVERSE, ["NOTLISTED"])


def test_selecting_a_subset_preserves_the_universe_order() -> None:
    """Selection narrows; it must not reorder."""
    assert select_instruments(UNIVERSE, ["hal", "sbin"]) == UNIVERSE


def test_no_symbols_means_the_whole_watchlist() -> None:
    """The default is the product question, not a special case."""
    assert select_instruments(UNIVERSE, None) == UNIVERSE


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


# --------------------------------------------------------------------------- #
# Market context beside the news
# --------------------------------------------------------------------------- #


def _bar(trading_date: date, close: str, volume: int = 1_000) -> DailyBarRevision:
    price = Decimal(close)
    return DailyBarRevision(
        instrument_id=SBIN.instrument_id,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="kite",
        source_instrument_id=1,
        candle=DailyCandle(
            trading_date=trading_date,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=volume,
            open_interest=None,
        ),
        retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        adjustment_status=AdjustmentStatus.RAW,
        completeness=BarCompleteness.COMPLETE,
        source_revision="a" * 64,
        batch_sha256="b" * 64,
        quality_revision="daily-bar-quality-v1",
    )


def _context(closes: list[str], *, last: date = date(2026, 8, 3)) -> MarketContext:
    count = len(closes)
    series = DailyBarSeries(
        bars=tuple(
            _bar(last - timedelta(days=count - 1 - index), close)
            for index, close in enumerate(closes)
        )
    )
    return summarise_recent_bars(series, as_of=date(2026, 8, 3))


def test_market_context_is_rendered_beside_the_news() -> None:
    """Two kinds of fact, side by side, neither presented as explaining the other."""
    digest = _digest(FRAUD)
    rendered = render_digest(digest, {SBIN.instrument_id: _context(["100", "110"])})

    assert "close 110" in rendered
    assert "+10.00%" in rendered
    assert "FRAUD_GOVERNANCE" in rendered


def test_a_fall_is_signed_so_it_cannot_read_as_a_rise() -> None:
    """An unsigned "10.00%" next to a governance finding invites a misreading."""
    rendered = render_digest(_digest(FRAUD), {SBIN.instrument_id: _context(["110", "99"])})

    assert "-10.00%" in rendered


def test_an_instrument_with_no_bars_says_so_rather_than_showing_nothing() -> None:
    """A blank where a price belongs reads as zero, which is a different claim."""
    absent = absent_context(HAL.instrument_id, as_of=date(2026, 8, 3), reason="nothing stored")
    rendered = render_digest(_digest(FRAUD), {HAL.instrument_id: absent})

    assert "no data -- nothing stored" in rendered


def test_a_stale_series_is_marked_in_the_line_a_reader_scans() -> None:
    """Presenting last month's close as today's is the worst failure available."""
    stale = _context(["100", "110"], last=date(2026, 7, 1))
    rendered = render_digest(_digest(FRAUD), {SBIN.instrument_id: stale})

    assert "[STALE]" in rendered
    assert "before cutoff" in rendered


def test_a_short_history_names_the_figure_it_could_not_compute() -> None:
    """A reason accompanies every n/a; a zero never stands in for a baseline."""
    rendered = render_digest(_digest(FRAUD), {SBIN.instrument_id: _context(["100", "110"])})

    assert "multi-day n/a" in rendered
    assert "5-session return needs" in rendered


def test_a_quiet_instrument_still_shows_its_market_context() -> None:
    """A quiet instrument that fell 4% is a different morning from one that did not move."""
    rendered = render_digest(_digest(), {SBIN.instrument_id: _context(["100", "110"])})

    assert "close 110" in rendered
    assert "nothing archived in this window" in rendered


def test_market_context_can_be_omitted_entirely() -> None:
    """--no-market reports archived news only, and says the field was not requested."""
    rendered = render_digest(_digest(FRAUD), None)

    assert "not requested" in rendered
    assert "close" not in rendered.split(DIGEST_DISCLAIMER)[-1].split("events")[0]


def test_an_instrument_missing_from_the_mapping_is_not_silently_blank() -> None:
    """A gap in the mapping is a different fact from an empty archive."""
    rendered = render_digest(_digest(FRAUD), {})

    assert "not requested" in rendered


def test_the_disclaimer_covers_the_market_figures_too() -> None:
    """Percentages beside headlines are the easiest thing to read as advice."""
    assert "not a view on value" in DIGEST_DISCLAIMER


def test_the_rendered_market_lines_contain_no_advice() -> None:
    """The same rule the news side is held to."""
    rendered = render_digest(_digest(), {SBIN.instrument_id: _context(["100", "110"])})
    framing = rendered.replace(DIGEST_DISCLAIMER, "").lower()

    for verb in _ADVICE:
        assert verb not in framing, f"the digest must not say {verb!r}"


@pytest.mark.parametrize("sessions", [0, -1, 1000])
def test_an_out_of_range_session_count_is_refused(sessions: int) -> None:
    """An unbounded multi-day window is an unbounded read."""
    with pytest.raises(ValidationError, match="sessions"):
        parse_sessions(sessions)


def test_the_session_count_defaults_to_the_documented_one() -> None:
    """One trading week, decided in the marketdata domain and used here."""
    args = cli.build_parser().parse_args(["--account", str(ACCOUNT)])

    assert args.sessions == DEFAULT_MULTI_DAY_SESSIONS
    assert parse_sessions(args.sessions) == DEFAULT_MULTI_DAY_SESSIONS


def test_market_context_can_be_switched_off_from_the_command_line() -> None:
    """An operator reading only the news should not pay for the bar read."""
    parser = cli.build_parser()

    assert parser.parse_args(["--account", str(ACCOUNT)]).no_market is False
    assert parser.parse_args(["--account", str(ACCOUNT), "--no-market"]).no_market is True


# --------------------------------------------------------------------------- #
# --ranked: the same path, with attention prepended
# --------------------------------------------------------------------------- #


def test_ranked_defaults_to_off() -> None:
    """The digest is the existing product; ranking is an addition, not a default."""
    parser = cli.build_parser()

    assert parser.parse_args(["--account", str(ACCOUNT)]).ranked is False
    assert parser.parse_args(["--account", str(ACCOUNT), "--ranked"]).ranked is True


def test_ranked_help_text_does_not_promise_a_recommendation() -> None:
    """Somebody reading --help must not come away thinking this is a signal."""
    parser = cli.build_parser()
    ranked_action = next(
        action for action in parser._actions if "--ranked" in action.option_strings
    )

    assert "recommendation" in (ranked_action.help or "")


def test_ranked_output_is_the_attention_rendering_prepended_to_the_unchanged_digest() -> None:
    """Exactly what run() assembles: unchanged digest, with attention ahead of it.

    ``run()`` composes ``render_attention`` and ``render_digest`` from the same
    ``digest``/``contexts`` pair and joins them with a blank line; that
    composition is reproduced here so it is proven without a database.
    """
    digest = _digest(FRAUD)
    contexts = {SBIN.instrument_id: _context(["100", "110"])}

    without_ranking = render_digest(digest, contexts)
    ranked = rank_watchlist(digest, contexts)
    with_ranking = f"{render_attention(ranked)}\n\n{without_ranking}"

    assert with_ranking.endswith(without_ranking)
    assert with_ranking.index(ATTENTION_DISCLAIMER) < with_ranking.index(DIGEST_DISCLAIMER)
    attention_only, _, _ = with_ranking.partition(without_ranking)
    assert "SBIN" in attention_only


def test_ranked_with_no_market_treats_every_instrument_as_unavailable() -> None:
    """--ranked and --no-market compose the same way render_digest already does.

    ``run()`` passes ``None`` for market contexts under ``--no-market`` to both
    ``render_digest`` and ``rank_watchlist`` -- attention must read that the same
    way the digest does: not requested, not zero.
    """
    ranked = rank_watchlist(_digest(FRAUD), None)

    assert all(entry.market_context_available is False for entry in ranked)
    rendered = render_attention(ranked)
    assert "market context unavailable" in rendered
