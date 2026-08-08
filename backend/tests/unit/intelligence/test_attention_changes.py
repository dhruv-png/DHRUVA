"""Rendered research changes are compact, mechanical, and never advice.

Digests, market contexts and rankings are built through the real machinery
(``build_digest``, ``MarketContext``, ``rank_watchlist``, ``compare_watchlist``)
for the same reason ``test_attention_brief.py`` does: a hand-labelled fixture
would let this module disagree with the types it actually renders and still
pass.
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
from dhruva.contexts.intelligence.domain.changes import compare_watchlist
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
    render_changes,
)
from dhruva.contexts.marketdata.domain.market_context import MarketContext, MarketDataAvailability
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

T1: Final = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)
T2: Final = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
PUBLISHED: Final = T1 - timedelta(hours=1)

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
UNIVERSE: Final = (SBIN, HAL)

COMMENTARY_HEADLINE: Final = "State Bank of India shares trade steady in early market activity"
#: Contains forbidden vocabulary deliberately -- a headline is quoted source
#: text and must not be rewritten to satisfy a wording test.
ADVICE_HEADLINE: Final = (
    "State Bank of India: analysts ask if it is time to buy this stock after the rally"
)

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


def _archived(url: str, title: str, *, first_seen_at: datetime) -> ArchivedNewsItem:
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
            analysed_at=first_seen_at,
        ),
    )


def _digest(*titled: tuple[str, datetime], known_at: datetime):  # type: ignore[no-untyped-def]
    """Build a digest the way a real read would: PIT-filtered before grouping."""
    items = [
        _archived(f"https://p.test/{n}", title, first_seen_at=first_seen_at)
        for n, (title, first_seen_at) in enumerate(titled)
        if first_seen_at <= known_at
    ]
    return build_digest(
        UNIVERSE,
        items,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=known_at,
    )


def _context(instrument: LinkableInstrument, *, one_day: str, as_of: date) -> MarketContext:
    return MarketContext(
        instrument_id=instrument.instrument_id,
        availability=MarketDataAvailability.AVAILABLE,
        as_of=as_of,
        latest_date=as_of,
        latest_close=Decimal("110.00"),
        previous_close=Decimal("100.00"),
        one_day_change_percent=Decimal(one_day),
        latest_volume=1_000,
        staleness_days=0,
        bars_available=2,
    )


def _rendered(
    *,
    before_titled: tuple[tuple[str, datetime], ...] = (),
    after_titled: tuple[tuple[str, datetime], ...] = (),
    before_contexts: dict[InstrumentId, MarketContext] | None = None,
    after_contexts: dict[InstrumentId, MarketContext] | None = None,
) -> str:
    before = rank_watchlist(_digest(*before_titled, known_at=T1), before_contexts)
    digest_after = _digest(*after_titled, known_at=T2)
    after = rank_watchlist(digest_after, after_contexts)
    changes = compare_watchlist(before, after, after_digest=digest_after)
    return render_changes(changes, from_cutoff=T1, to_cutoff=T2)


# --------------------------------------------------------------------------- #
# The empty and whole-report cases
# --------------------------------------------------------------------------- #


def test_the_disclaimer_is_reused() -> None:
    """One disclaimer, not a second one that could drift from the first."""
    rendered = _rendered(
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    assert ATTENTION_DISCLAIMER in rendered


def test_the_cutoffs_are_printed() -> None:
    """The reader must be able to see exactly which two instants were compared."""
    rendered = _rendered()

    assert T1.isoformat() in rendered
    assert T2.isoformat() in rendered


def test_no_changes_states_so_rather_than_a_blank_page() -> None:
    """Identical states compared is a complete, correct, and quiet answer."""
    rendered = _rendered()

    assert "No changes between these two cutoffs." in rendered


def test_rendering_is_byte_for_byte_deterministic() -> None:
    """A pure function of an already-computed comparison: same input, same output."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}

    first = _rendered(after_contexts=contexts)
    second = _rendered(after_contexts=contexts)

    assert first == second


def test_rendering_preserves_the_comparisons_own_order() -> None:
    """Sorting is compare_watchlist's job; rendering must not silently re-sort."""
    rendered = _rendered(
        after_contexts={
            SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date()),
            HAL.instrument_id: _context(HAL, one_day="2.10", as_of=T2.date()),
        }
    )

    assert rendered.index("SBIN") < rendered.index("HAL")


# --------------------------------------------------------------------------- #
# What one instrument's entry shows
# --------------------------------------------------------------------------- #


def test_categories_are_named_explicitly() -> None:
    """A reader (or a script) can check the category list mechanically."""
    rendered = _rendered(
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    assert "categories: ENTERED, SCORE_INCREASED, BAND_CHANGED" in rendered


def test_the_attention_transition_is_shown() -> None:
    """Band and score, before and after, on one line."""
    rendered = _rendered(
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    assert "attention: LOW 0 -> ELEVATED 3" in rendered


def test_new_reasons_are_listed() -> None:
    """The mechanical facts that changed, in the ranking's own wording."""
    rendered = _rendered(
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    assert "new reasons:" in rendered
    assert "1-day absolute move 8.50%" in rendered


def test_new_archived_news_shows_category_sentiment_headline_and_source() -> None:
    """Exactly the fields the brief also shows, plus when it was first seen."""
    rendered = _rendered(after_titled=((COMMENTARY_HEADLINE, T1 + timedelta(hours=1)),))

    assert "new archived news:" in rendered
    assert "GENERAL_COMMENTARY" in rendered
    assert "sentiment" in rendered
    assert COMMENTARY_HEADLINE in rendered
    assert "https://p.test/0" in rendered
    assert "gdelt" in rendered
    assert f"first seen {(T1 + timedelta(hours=1)).isoformat()}" in rendered


def test_market_context_added_is_shown() -> None:
    """Absent at T1, present at T2, stated as the transition it is."""
    rendered = _rendered(
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="0.10", as_of=T2.date())}
    )

    assert "market context: unavailable -> available" in rendered


def test_market_context_removed_is_shown() -> None:
    """Present at T1, absent at T2, stated as the transition it is."""
    rendered = _rendered(
        before_contexts={SBIN.instrument_id: _context(SBIN, one_day="0.10", as_of=T1.date())}
    )

    assert "market context: available -> unavailable" in rendered


# --------------------------------------------------------------------------- #
# Output safety
# --------------------------------------------------------------------------- #


def test_a_headline_containing_forbidden_words_is_not_rewritten() -> None:
    """Quoted source text is exempt from DHRUVA's own wording rule -- and shown as-is."""
    rendered = _rendered(after_titled=((ADVICE_HEADLINE, T1 + timedelta(hours=1)),))

    assert ADVICE_HEADLINE in rendered


def test_dhruvas_own_wording_contains_no_advice_language() -> None:
    """DHRUVA's own framing must avoid these words; a quoted headline is exempt."""
    rendered = _rendered(
        after_titled=((ADVICE_HEADLINE, T1 + timedelta(hours=1)),),
        after_contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())},
    )

    framing = rendered.replace(ATTENTION_DISCLAIMER, "").replace(ADVICE_HEADLINE, "").lower()
    for word in _FORBIDDEN:
        assert word not in framing, f"the change report must not say {word!r}"
