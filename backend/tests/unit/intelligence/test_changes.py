"""Research changes are deterministic, PIT-correct, and never a recommendation.

Digests and market contexts are built through the real ``build_digest``/
``MarketContext``/``rank_watchlist`` machinery, for the same reason
``test_attention.py`` does: a hand-labelled fixture would let this module
disagree with the domain types it actually consumes and still pass.
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
from dhruva.contexts.intelligence.domain.attention import AttentionBand, ResearchAttention
from dhruva.contexts.intelligence.domain.changes import (
    ChangeCategory,
    ResearchChange,
    compare_instrument,
    compare_watchlist,
    new_news_since,
)
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
from dhruva.contexts.intelligence.interfaces.attention_presentation import rank_watchlist
from dhruva.contexts.marketdata.domain.market_context import MarketContext, MarketDataAvailability
from dhruva.shared.identity import InstrumentId
from dhruva.shared.invariants import InvariantViolation

pytestmark = pytest.mark.unit

T1: Final = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)
T2: Final = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
PUBLISHED: Final = T1 - timedelta(hours=1)

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project",
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

FRAUD_HEADLINE: Final = "State Bank of India faces a forensic audit over accounting irregularities"
COMMENTARY_HEADLINE: Final = "State Bank of India shares trade steady in early market activity"


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
    """Build a digest the way a real read would: PIT-filtered before grouping.

    ``build_digest`` performs no point-in-time filtering itself -- that is
    the repository's job -- so this reproduces it exactly, as
    ``test_attention.py`` does, rather than accidentally proving something
    only true of an unfiltered digest.
    """
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


def _only(changes: tuple[ResearchChange, ...], symbol: str) -> ResearchChange:
    return next(change for change in changes if change.canonical_symbol == symbol)


def _attention(
    *,
    known_at: datetime,
    contexts: dict[InstrumentId, MarketContext] | None,
    titled: tuple[tuple[str, datetime], ...] = (),
) -> tuple[ResearchAttention, ...]:
    return rank_watchlist(_digest(*titled, known_at=known_at), contexts)


# --------------------------------------------------------------------------- #
# The whole-report cases
# --------------------------------------------------------------------------- #


def test_identical_cutoffs_produce_no_changes() -> None:
    """The same state read twice differs in nothing -- an empty, correct answer."""
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    before = _attention(known_at=T1, contexts=contexts)
    digest = _digest(known_at=T1)

    changes = compare_watchlist(before, before, after_digest=digest)

    assert changes == ()


def test_changes_are_ordered_by_the_size_of_the_score_movement() -> None:
    """The biggest mover is first, ties broken alphabetically."""
    before = _attention(known_at=T1, contexts={})
    after = _attention(
        known_at=T2,
        contexts={
            SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date()),
            HAL.instrument_id: _context(HAL, one_day="2.10", as_of=T2.date()),
        },
    )
    digest_after = _digest(known_at=T2)

    changes = compare_watchlist(before, after, after_digest=digest_after)

    assert [change.canonical_symbol for change in changes] == ["SBIN", "HAL"]


def test_repeated_comparison_is_byte_for_byte_deterministic() -> None:
    """A pure function of two already-computed states: same input, same output."""
    before = _attention(known_at=T1, contexts={})
    after = _attention(
        known_at=T2, contexts={SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )
    digest_after = _digest(known_at=T2)

    first = compare_watchlist(before, after, after_digest=digest_after)
    second = compare_watchlist(before, after, after_digest=digest_after)

    assert first == second


def test_an_instrument_only_present_before_is_skipped_rather_than_guessed_at() -> None:
    """A watchlist that shrank between the two cutoffs is out of this module's scope."""
    before_context = {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    before = _attention(known_at=T1, contexts=before_context)
    after = tuple(
        entry for entry in _attention(known_at=T2, contexts={}) if entry.canonical_symbol != "SBIN"
    )
    digest_after = _digest(known_at=T2)

    changes = compare_watchlist(before, after, after_digest=digest_after)

    assert all(change.canonical_symbol != "SBIN" for change in changes)


# --------------------------------------------------------------------------- #
# Score, band, and the attention set
# --------------------------------------------------------------------------- #


def test_a_score_increase_is_detected() -> None:
    """A higher score at the later cutoff is reported as SCORE_INCREASED."""
    before = rank_watchlist(_digest(known_at=T1), {})
    after = rank_watchlist(
        _digest(known_at=T2), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.SCORE_INCREASED in change.categories


def test_a_score_decrease_is_detected() -> None:
    """A lower score at the later cutoff is reported as SCORE_DECREASED."""
    before = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    )
    after = rank_watchlist(_digest(known_at=T2), {})

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.SCORE_DECREASED in change.categories


def test_a_band_change_is_detected() -> None:
    """A band that differs between the two cutoffs is reported as BAND_CHANGED."""
    before = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="2.50", as_of=T1.date())}
    )
    after = rank_watchlist(
        _digest(known_at=T2), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    before_sbin = next(e for e in before if e.canonical_symbol == "SBIN")
    after_sbin = next(e for e in after if e.canonical_symbol == "SBIN")
    assert before_sbin.band is AttentionBand.NORMAL
    assert after_sbin.band is AttentionBand.ELEVATED

    change = compare_instrument(before_sbin, after_sbin)

    assert change is not None
    assert ChangeCategory.BAND_CHANGED in change.categories


def test_entering_the_attention_set_is_detected() -> None:
    """Score 0 at T1, positive at T2: ENTERED, not merely SCORE_INCREASED."""
    before = rank_watchlist(_digest(known_at=T1), {})
    after = rank_watchlist(
        _digest(known_at=T2), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.ENTERED in change.categories
    assert ChangeCategory.EXITED not in change.categories


def test_leaving_the_attention_set_is_detected() -> None:
    """Score positive at T1, 0 at T2: EXITED, not merely SCORE_DECREASED."""
    before = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    )
    after = rank_watchlist(_digest(known_at=T2), {})

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.EXITED in change.categories
    assert ChangeCategory.ENTERED not in change.categories


def test_new_reasons_are_only_those_absent_at_the_earlier_cutoff() -> None:
    """A reason present at both cutoffs is not "new"."""
    before = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="2.50", as_of=T1.date())}
    )
    after = rank_watchlist(
        _digest(known_at=T2), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T2.date())}
    )

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert "1-day absolute move 8.50%" in change.new_reasons
    assert "1-day absolute move 2.50%" not in change.new_reasons


def test_nothing_changed_yields_no_reported_change() -> None:
    """Two identical verdicts compare to no change at all, not an empty one."""
    entry = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    )
    sbin = next(e for e in entry if e.canonical_symbol == "SBIN")

    assert compare_instrument(sbin, sbin) is None


# --------------------------------------------------------------------------- #
# Market context availability
# --------------------------------------------------------------------------- #


def test_market_context_becoming_available_is_detected() -> None:
    """Absent at T1, present at T2: MARKET_CONTEXT_ADDED, never a fabricated flat."""
    before = rank_watchlist(_digest(known_at=T1), {})
    after = rank_watchlist(
        _digest(known_at=T2), {SBIN.instrument_id: _context(SBIN, one_day="0.10", as_of=T2.date())}
    )

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.MARKET_CONTEXT_ADDED in change.categories


def test_market_context_becoming_unavailable_is_detected() -> None:
    """Present at T1, absent at T2: MARKET_CONTEXT_REMOVED, distinct from EXITED."""
    before = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="0.10", as_of=T1.date())}
    )
    after = rank_watchlist(_digest(known_at=T2), {})

    change = compare_instrument(
        next(e for e in before if e.canonical_symbol == "SBIN"),
        next(e for e in after if e.canonical_symbol == "SBIN"),
    )

    assert change is not None
    assert ChangeCategory.MARKET_CONTEXT_REMOVED in change.categories


# --------------------------------------------------------------------------- #
# News: first seen since the earlier cutoff
# --------------------------------------------------------------------------- #


def test_news_first_seen_between_the_two_cutoffs_is_new() -> None:
    """The property the whole comparison exists for: T1 < first_seen_at <= T2."""
    digest_after = _digest((FRAUD_HEADLINE, T1 + timedelta(hours=1)), known_at=T2)
    section = next(s for s in digest_after.sections if s.canonical_symbol == "SBIN")

    new = new_news_since(section, since=T1)

    assert len(new) == 1
    assert new[0].item.revision.item.text.title == FRAUD_HEADLINE


def test_news_first_seen_before_the_earlier_cutoff_is_not_new() -> None:
    """Already knowable at T1: not new, even though it is present at T2."""
    digest_after = _digest((FRAUD_HEADLINE, T1 - timedelta(minutes=5)), known_at=T2)
    section = next(s for s in digest_after.sections if s.canonical_symbol == "SBIN")

    assert new_news_since(section, since=T1) == ()


def test_news_added_is_reported_alongside_the_attention_change() -> None:
    """A new item's category, headline and identity ride along with the change."""
    before = rank_watchlist(_digest(known_at=T1), {})
    digest_after = _digest((FRAUD_HEADLINE, T1 + timedelta(hours=1)), known_at=T2)
    after = rank_watchlist(digest_after, {})

    changes = compare_watchlist(before, after, after_digest=digest_after)

    sbin = _only(changes, "SBIN")
    assert ChangeCategory.NEWS_ADDED in sbin.categories
    assert len(sbin.new_news) == 1
    assert sbin.new_news[0].item.revision.item.text.title == FRAUD_HEADLINE


def test_news_added_is_reported_even_when_the_item_count_cap_hides_a_score_change() -> None:
    """A capped news score can hide a genuinely new item; NEWS_ADDED must not.

    Three items are already at the news-count cap before T1; a fourth,
    genuinely new item at T2 adds no further score points, but it is still
    new and must still be reported.
    """
    early = [
        (f"State Bank of India note {n}", T1 - timedelta(minutes=10 * (n + 1))) for n in range(3)
    ]
    before = rank_watchlist(_digest(*early, known_at=T1), {})
    digest_after = _digest(*early, (COMMENTARY_HEADLINE, T1 + timedelta(hours=1)), known_at=T2)
    after = rank_watchlist(digest_after, {})

    before_sbin = next(e for e in before if e.canonical_symbol == "SBIN")
    after_sbin = next(e for e in after if e.canonical_symbol == "SBIN")
    assert before_sbin.score == after_sbin.score  # the cap absorbed the fourth item

    change = compare_instrument(
        before_sbin,
        after_sbin,
        new_news=new_news_since(
            next(s for s in digest_after.sections if s.canonical_symbol == "SBIN"), since=T1
        ),
    )

    assert change is not None
    assert ChangeCategory.NEWS_ADDED in change.categories
    assert ChangeCategory.SCORE_INCREASED not in change.categories


# --------------------------------------------------------------------------- #
# Bounds and invariants
# --------------------------------------------------------------------------- #


def test_a_change_with_no_categories_is_refused() -> None:
    """The invariant a caller could otherwise violate by constructing one by hand."""
    entry = rank_watchlist(
        _digest(known_at=T1), {SBIN.instrument_id: _context(SBIN, one_day="8.50", as_of=T1.date())}
    )
    sbin = next(e for e in entry if e.canonical_symbol == "SBIN")

    with pytest.raises(InvariantViolation, match="at least one category"):
        ResearchChange(
            instrument_id=sbin.instrument_id,
            canonical_symbol=sbin.canonical_symbol,
            company_name=sbin.company_name,
            before=sbin,
            after=sbin,
            categories=(),
            new_reasons=(),
            new_news=(),
        )
