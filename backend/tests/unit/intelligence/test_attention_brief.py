"""The brief is a compact, deterministic substitute for the digest, never advice.

Digests, market contexts and rankings are built through the real machinery
(``build_digest``, ``MarketContext``, ``rank_watchlist``, ``top_attention``)
for the same reason ``test_attention.py`` does: a hand-labelled fixture would
let this module disagree with the types it actually renders and still pass.
"""

from __future__ import annotations

import hashlib
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
from dhruva.contexts.intelligence.domain.attention import top_attention
from dhruva.contexts.intelligence.domain.digest import build_digest
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument, link_entities
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
    render_brief,
)
from dhruva.contexts.marketdata.domain.market_context import MarketContext, MarketDataAvailability
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
LATE_SEEN: Final = ANALYSED + timedelta(hours=6)

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project via economictimes.indiatimes.test",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://gdeltproject.org",
)
SBIN: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "sbin"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
HAL: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
)
COCHINSHIP: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "cochinship"),
    canonical_symbol="COCHINSHIP",
    company_name="Cochin Shipyard Limited",
)
UNIVERSE: Final = (SBIN, HAL, COCHINSHIP)

COMMENTARY_HEADLINE: Final = "State Bank of India shares trade steady in early market activity"
#: Contains forbidden vocabulary deliberately -- a headline is quoted source
#: text and must not be rewritten to satisfy a wording test.
ADVICE_HEADLINE: Final = (
    "State Bank of India: analysts ask if it is time to buy this stock after the rally"
)

#: The forbidden-term list this slice was asked to hold DHRUVA's own wording
#: to. Distinct from (and a superset in spirit of) the older ``_ADVICE`` tuple
#: elsewhere in the suite; kept local because this slice named it explicitly.
_FORBIDDEN = (
    "buy",
    "sell",
    "hold",
    "target",
    "upside",
    "downside",
    "best stock",
    "opportunity",
    "conviction",
    "expected return",
)


def _archived(url: str, title: str, *, first_seen_at: datetime = SEEN) -> ArchivedNewsItem:
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
        first_seen_at=first_seen_at,
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
            analysed_at=max(first_seen_at, ANALYSED),
        ),
    )


def _digest(*titles: str, known_at: datetime = ANALYSED, first_seen_at: datetime = SEEN):  # type: ignore[no-untyped-def]
    """Build a digest the way a real read would: PIT-filtered before grouping.

    ``build_digest`` performs no point-in-time filtering itself -- that is the
    repository's job -- so this reproduces it, exactly as ``test_attention.py``
    does, rather than accidentally proving something only true of an
    unfiltered digest.
    """
    items = [
        _archived(f"https://p.test/{n}", t, first_seen_at=first_seen_at)
        for n, t in enumerate(titles)
        if first_seen_at <= known_at
    ]
    return build_digest(
        UNIVERSE,
        items,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=known_at,
    )


def _context(instrument: LinkableInstrument = SBIN, *, one_day: str = "5.84") -> MarketContext:
    return MarketContext(
        instrument_id=instrument.instrument_id,
        availability=MarketDataAvailability.AVAILABLE,
        as_of=date(2026, 8, 3),
        latest_date=date(2026, 8, 3),
        latest_close=Decimal("110.00"),
        previous_close=Decimal("100.00"),
        one_day_change_percent=Decimal(one_day),
        latest_volume=1_000,
        staleness_days=0,
        bars_available=2,
    )


def _brief(
    *titles: str,
    contexts: dict[InstrumentId, MarketContext] | None = None,
    top: int = 5,
    known_at: datetime = ANALYSED,
    first_seen_at: datetime = SEEN,
) -> str:
    digest = _digest(*titles, known_at=known_at, first_seen_at=first_seen_at)
    ranked = rank_watchlist(digest, contexts)
    selected = top_attention(ranked, limit=top)
    return render_brief(selected, digest=digest, contexts=contexts, total_ranked=len(ranked))


# --------------------------------------------------------------------------- #
# Ordering, ties and top-N selection
# --------------------------------------------------------------------------- #


def test_the_brief_preserves_rank_order() -> None:
    """The brief shows the ranking's own order; it does not re-sort."""
    contexts = {
        SBIN.instrument_id: _context(SBIN, one_day="8.50"),
        HAL.instrument_id: _context(HAL, one_day="2.10"),
    }
    rendered = _brief(contexts=contexts)

    assert rendered.index("SBIN") < rendered.index("HAL")


def test_the_brief_numbers_entries_from_one() -> None:
    """A reader scans a numbered list; it must start where they expect."""
    contexts = {SBIN.instrument_id: _context(SBIN)}
    rendered = _brief(contexts=contexts)

    assert "1. SBIN" in rendered


def test_fewer_than_the_default_top_shows_only_what_is_noteworthy() -> None:
    """Only one instrument moved; the brief shows one, not five padded slots."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}
    rendered = _brief(contexts=contexts, top=5)

    assert "1. SBIN" in rendered
    assert "2." not in rendered
    assert f"1 of {len(UNIVERSE)} watchlist instrument shown" in rendered


def test_an_explicit_top_bounds_how_many_entries_are_shown() -> None:
    """--top 1 shows exactly one, even when more are noteworthy."""
    contexts = {
        SBIN.instrument_id: _context(SBIN, one_day="8.50"),
        HAL.instrument_id: _context(HAL, one_day="6.00"),
    }
    rendered = _brief(contexts=contexts, top=1)

    assert "1. SBIN" in rendered
    assert "HAL" not in rendered
    assert f"1 of {len(UNIVERSE)} watchlist instrument shown" in rendered


def test_the_shown_count_is_pluralised_correctly() -> None:
    """More than one entry shown reads as "instruments", not a copy-paste singular."""
    contexts = {
        SBIN.instrument_id: _context(SBIN, one_day="8.50"),
        HAL.instrument_id: _context(HAL, one_day="6.00"),
    }
    rendered = _brief(contexts=contexts, top=5)

    assert f"2 of {len(UNIVERSE)} watchlist instruments shown" in rendered


def test_nothing_noteworthy_states_so_rather_than_an_empty_page() -> None:
    """A quiet watchlist is a valid, complete answer, stated in words."""
    rendered = _brief()

    assert "No instrument has observable research attention at this cutoff." in rendered


def test_an_empty_watchlist_states_so_distinctly_from_a_quiet_one() -> None:
    """Nobody being followed is a different fact from nobody moving."""
    empty_digest = build_digest(
        (), (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED
    )
    rendered = render_brief((), digest=empty_digest, contexts=None, total_ranked=0)

    assert "No instruments are on the watchlist at this cutoff." in rendered


# --------------------------------------------------------------------------- #
# Market context: shown from the same PIT-resolved data, never fabricated
# --------------------------------------------------------------------------- #


def test_market_close_and_date_are_shown_from_the_passed_context() -> None:
    """The brief prints exactly what the caller's own context carried."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}
    rendered = _brief(contexts=contexts)

    assert "close 110.00 on 2026-08-03" in rendered


def test_market_context_not_requested_is_shown_as_such() -> None:
    """--no-market's ``None`` must read as "not requested", never as flat.

    News gives SBIN a non-zero score so it appears in the brief at all; market
    context being ``None`` (as ``--no-market`` passes it) must not be read as
    a computed, unchanged close.
    """
    rendered = _brief(COMMENTARY_HEADLINE, contexts=None)

    assert "market:" in rendered
    assert "not requested" in rendered
    assert "close" not in rendered.split("market:")[1].split("news:")[0]


def test_a_no_data_context_names_the_reason_rather_than_a_close() -> None:
    """NO_DATA is DHRUVA not knowing, never a computed zero-movement close."""
    absent = MarketContext(
        instrument_id=SBIN.instrument_id,
        availability=MarketDataAvailability.NO_DATA,
        as_of=date(2026, 8, 3),
        limitation="nothing stored yet",
    )
    rendered = _brief(COMMENTARY_HEADLINE, contexts={SBIN.instrument_id: absent})

    assert "no data -- nothing stored yet" in rendered
    assert "close" not in rendered.split("market:")[1].split("news:")[0]


# --------------------------------------------------------------------------- #
# News: the minimal, citable facts already stored
# --------------------------------------------------------------------------- #


def test_no_archived_news_states_so() -> None:
    """A quiet news side is stated, not left as a blank the reader must parse."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}
    rendered = _brief(contexts=contexts)

    assert "none archived" in rendered


def test_archived_news_shows_category_sentiment_headline_and_source() -> None:
    """Exactly the fields this slice asked for -- nothing more, nothing rewritten."""
    rendered = _brief(COMMENTARY_HEADLINE)

    assert "GENERAL_COMMENTARY" in rendered
    assert "sentiment" in rendered
    assert COMMENTARY_HEADLINE in rendered
    assert PUBLISHED.isoformat() in rendered
    assert "gdelt" in rendered
    assert "https://p.test/0" in rendered


def test_a_news_item_first_seen_after_the_cutoff_is_absent() -> None:
    """A historical cutoff earlier than an item's own first_seen_at must not see it.

    ``build_digest`` already enforces this by construction; the brief must
    inherit it rather than performing a second, independent read.
    """
    early = _brief(COMMENTARY_HEADLINE, known_at=PUBLISHED, first_seen_at=LATE_SEEN, top=5)
    late = _brief(COMMENTARY_HEADLINE, known_at=LATE_SEEN, first_seen_at=LATE_SEEN, top=5)

    assert "No instrument has observable research attention" in early
    assert COMMENTARY_HEADLINE in late


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_rendering_is_byte_for_byte_deterministic() -> None:
    """A pure function of already-stored facts: same input, identical output."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}

    first = _brief(COMMENTARY_HEADLINE, contexts=contexts)
    second = _brief(COMMENTARY_HEADLINE, contexts=contexts)

    assert first == second


# --------------------------------------------------------------------------- #
# Output safety: DHRUVA's own wording, never a headline rewritten to hide it
# --------------------------------------------------------------------------- #


def test_the_disclaimer_is_reused_in_the_brief() -> None:
    """One disclaimer, not a second one that could drift from the first."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}
    assert ATTENTION_DISCLAIMER in _brief(contexts=contexts)


def test_a_headline_containing_forbidden_words_is_not_rewritten() -> None:
    """Quoted source text is exempt from DHRUVA's own wording rule -- and shown as-is."""
    rendered = _brief(ADVICE_HEADLINE)

    assert ADVICE_HEADLINE in rendered


def test_dhruvas_own_wording_contains_no_advice_language() -> None:
    """DHRUVA's own framing must avoid these words; a quoted headline is exempt."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50")}
    rendered = _brief(ADVICE_HEADLINE, contexts=contexts)

    framing = rendered.replace(ATTENTION_DISCLAIMER, "").replace(ADVICE_HEADLINE, "").lower()
    for word in _FORBIDDEN:
        assert word not in framing, f"the brief must not say {word!r}"
