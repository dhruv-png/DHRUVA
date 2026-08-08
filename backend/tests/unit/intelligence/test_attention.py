"""Research attention is deterministic, symmetric, and never a recommendation.

Digests are built through the real ``build_digest`` and market contexts through
the real ``MarketContext``/``summarise_recent_bars`` machinery wherever
practical, for the same reason ``test_digest_cli.py`` does: a hand-labelled
fixture would let this module disagree with the domain types it actually
consumes and still pass.
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
from dhruva.contexts.intelligence.domain.attention import (
    MAX_ATTENTION_SCORE,
    AttentionBand,
    ResearchAttention,
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

PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
LATE_SEEN: Final = ANALYSED + timedelta(hours=6)

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
    aliases=("Hindustan Aeronautics",),
)
COCHINSHIP: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "cochinship"),
    canonical_symbol="COCHINSHIP",
    company_name="Cochin Shipyard Limited",
)
UNIVERSE: Final = (SBIN, HAL, COCHINSHIP)

FRAUD_HEADLINE: Final = "State Bank of India faces a forensic audit over accounting irregularities"
COMMENTARY_HEADLINE: Final = "State Bank of India shares trade steady in early market activity"


def _archived(
    url: str,
    title: str,
    *,
    first_seen_at: datetime = SEEN,
    universe: tuple[LinkableInstrument, ...] = UNIVERSE,
) -> ArchivedNewsItem:
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
            mapping=link_entities(title, universe=universe, published_on=PUBLISHED.date()),
            analysed_at=max(first_seen_at, ANALYSED),
        ),
    )


def _digest(*titles: str, known_at: datetime = ANALYSED, first_seen_at: datetime = SEEN):  # type: ignore[no-untyped-def]
    """Build a digest the way a real read would: PIT-filtered before grouping.

    ``build_digest`` itself performs no point-in-time filtering -- that is the
    repository's job (``GetArchivedNews``/``list_known_at``), enforced and
    tested there. Reproducing exactly that filter here, rather than skipping
    it, is what lets a test in this file prove attention correctly reflects a
    properly PIT-filtered digest instead of accidentally proving something
    only true of an unfiltered one.
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


def _context(  # noqa: PLR0913 - one keyword per independently varied factor
    instrument: LinkableInstrument = SBIN,
    *,
    one_day: str | None = None,
    multi_day: str | None = None,
    sessions: int | None = None,
    volume_ratio: str | None = None,
    baseline_sessions: int | None = None,
    availability: MarketDataAvailability = MarketDataAvailability.AVAILABLE,
    reason: str = "no data",
) -> MarketContext:
    if availability is MarketDataAvailability.NO_DATA:
        return MarketContext(
            instrument_id=instrument.instrument_id,
            availability=MarketDataAvailability.NO_DATA,
            as_of=date(2026, 8, 3),
            limitation=reason,
        )
    return MarketContext(
        instrument_id=instrument.instrument_id,
        availability=availability,
        as_of=date(2026, 8, 3),
        latest_date=date(2026, 8, 3),
        latest_close=Decimal("100.00"),
        previous_close=Decimal("95.00") if one_day is not None else None,
        one_day_change_percent=Decimal(one_day) if one_day is not None else None,
        multi_day_change_percent=Decimal(multi_day) if multi_day is not None else None,
        multi_day_sessions=sessions,
        latest_volume=1_000,
        volume_ratio=Decimal(volume_ratio) if volume_ratio is not None else None,
        volume_baseline_sessions=baseline_sessions,
        staleness_days=0,
        bars_available=6,
    )


def _only(ranked: tuple[ResearchAttention, ...], symbol: str) -> ResearchAttention:
    return next(item for item in ranked if item.canonical_symbol == symbol)


# --------------------------------------------------------------------------- #
# Deterministic ordering and ties
# --------------------------------------------------------------------------- #


def test_higher_score_sorts_first() -> None:
    """The whole point: more observable change/coverage sorts to the top."""
    digest = _digest()
    contexts = {
        SBIN.instrument_id: _context(SBIN, one_day="8.50"),
        HAL.instrument_id: _context(HAL, one_day="2.10"),
        COCHINSHIP.instrument_id: _context(COCHINSHIP),
    }

    ranked = rank_watchlist(digest, contexts)

    assert [item.canonical_symbol for item in ranked] == ["SBIN", "HAL", "COCHINSHIP"]
    assert ranked[0].score > ranked[1].score > ranked[2].score


def test_equal_scores_break_ties_alphabetically() -> None:
    """A deterministic tie-break, not the order instruments happened to be read in."""
    digest = _digest()
    contexts = {
        SBIN.instrument_id: _context(SBIN),
        HAL.instrument_id: _context(HAL),
        COCHINSHIP.instrument_id: _context(COCHINSHIP),
    }

    ranked = rank_watchlist(digest, contexts)

    assert [item.canonical_symbol for item in ranked] == ["COCHINSHIP", "HAL", "SBIN"]
    assert {item.score for item in ranked} == {0}


def test_ranking_is_stable_across_repeated_runs() -> None:
    """A pure function over already-stored facts: same input, byte-identical output."""
    digest = _digest(FRAUD_HEADLINE)
    contexts = {SBIN.instrument_id: _context(SBIN, one_day="4.00")}

    first = rank_watchlist(digest, contexts)
    second = rank_watchlist(digest, contexts)

    assert first == second


# --------------------------------------------------------------------------- #
# Symmetric price movement
# --------------------------------------------------------------------------- #


def test_a_rise_and_an_equal_fall_produce_identical_attention() -> None:
    """+5% and -5% are the same magnitude of movement, and score identically."""
    digest = _digest()
    rising = rank_watchlist(digest, {SBIN.instrument_id: _context(SBIN, one_day="5.84")})
    falling = rank_watchlist(digest, {SBIN.instrument_id: _context(SBIN, one_day="-5.84")})

    rise = _only(rising, "SBIN")
    fall = _only(falling, "SBIN")
    assert rise.score == fall.score
    assert rise.band is fall.band
    assert rise.reasons == ("1-day absolute move 5.84%",)
    assert fall.reasons == ("1-day absolute move 5.84%",)


def test_a_move_below_the_noteworthy_floor_contributes_nothing() -> None:
    """Most sessions move a little; that alone must not read as attention-worthy."""
    digest = _digest()
    ranked = rank_watchlist(digest, {SBIN.instrument_id: _context(SBIN, one_day="0.07")})

    entry = _only(ranked, "SBIN")
    assert entry.score == 0
    assert entry.band is AttentionBand.LOW
    assert entry.reasons == ()


@pytest.mark.parametrize(
    ("magnitude", "expected_points"),
    [("1.99", 0), ("2.00", 1), ("4.99", 1), ("5.00", 2), ("7.99", 2), ("8.00", 3), ("15.00", 3)],
)
def test_move_point_tiers_are_exact(magnitude: str, expected_points: int) -> None:
    """Each tier boundary is tested on both sides, not just in the middle."""
    digest = _digest()
    ranked = rank_watchlist(digest, {SBIN.instrument_id: _context(SBIN, one_day=magnitude)})

    assert _only(ranked, "SBIN").score == expected_points


def test_one_day_and_multi_day_moves_both_contribute_independently() -> None:
    """A live shape: a 1-day move and a 5-session move are two separate facts."""
    digest = _digest()
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, one_day="5.84", multi_day="5.54", sessions=5)},
    )

    entry = _only(ranked, "SBIN")
    assert "1-day absolute move 5.84%" in entry.reasons
    assert "5-session absolute move 5.54%" in entry.reasons
    assert entry.score == 2 + 2


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


def test_elevated_volume_contributes_attention() -> None:
    """A live shape: volume well above the recent baseline is worth naming."""
    digest = _digest()
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, volume_ratio="2.92", baseline_sessions=20)},
    )

    entry = _only(ranked, "SBIN")
    assert entry.score == 2
    assert entry.reasons == ("volume 2.92x prior-20-session mean",)


def test_volume_at_or_below_normal_contributes_nothing() -> None:
    """Only excess over the baseline is a factor; quieter-than-usual is not scored."""
    digest = _digest()
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, volume_ratio="0.50", baseline_sessions=20)},
    )

    assert _only(ranked, "SBIN").score == 0


@pytest.mark.parametrize(
    ("ratio", "expected_points"),
    [("1.24", 0), ("1.25", 1), ("1.99", 1), ("2.00", 2), ("2.99", 2), ("3.00", 3)],
)
def test_volume_point_tiers_are_exact(ratio: str, expected_points: int) -> None:
    """Each volume tier boundary is tested on both sides."""
    digest = _digest()
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, volume_ratio=ratio, baseline_sessions=20)},
    )

    assert _only(ranked, "SBIN").score == expected_points


# --------------------------------------------------------------------------- #
# News presence and event class
# --------------------------------------------------------------------------- #


def test_recent_archived_news_contributes_attention() -> None:
    """Even plain commentary in the archive is worth surfacing over silence."""
    digest = _digest(COMMENTARY_HEADLINE)
    ranked = rank_watchlist(digest, {})

    entry = _only(ranked, "SBIN")
    assert entry.score >= 1
    assert "1 recent archived news item" in entry.reasons


def test_news_item_count_is_reported_and_capped() -> None:
    """A single very newsy instrument must not dominate the ranking on count alone."""
    digest = _digest(*([COMMENTARY_HEADLINE] * 5))
    ranked = rank_watchlist(digest, {})

    entry = _only(ranked, "SBIN")
    assert "5 recent archived news items" in entry.reasons
    # Five items exist, but the point contribution is capped at three.
    news_points = 3
    assert entry.score == news_points


def test_a_notable_event_category_earns_a_bonus_over_commentary() -> None:
    """A fraud finding outranks plain commentary at an equal item count."""
    fraud_digest = _digest(FRAUD_HEADLINE)
    commentary_digest = _digest(COMMENTARY_HEADLINE)

    fraud = _only(rank_watchlist(fraud_digest, {}), "SBIN")
    commentary = _only(rank_watchlist(commentary_digest, {}), "SBIN")

    assert fraud.score > commentary.score
    assert any("event class" in reason for reason in fraud.reasons)
    assert any("event class GENERAL_COMMENTARY" in reason for reason in commentary.reasons)


def test_instruments_with_no_news_remain_valid() -> None:
    """Absence of coverage is a normal, valid answer -- not a defect."""
    digest = _digest()
    ranked = rank_watchlist(digest, {})

    entry = _only(ranked, "HAL")
    assert entry.reasons == ()
    assert entry.score == 0
    assert entry.band is AttentionBand.LOW


# --------------------------------------------------------------------------- #
# Point-in-time correctness
# --------------------------------------------------------------------------- #


def test_an_item_first_seen_after_the_cutoff_contributes_nothing() -> None:
    """A historical cutoff earlier than an item's own first_seen_at must not see it.

    build_digest already enforces this by construction -- the item simply is
    not stored in any section at that known_at -- and this proves attention
    inherits that guarantee rather than quietly bypassing it.
    """
    early_cutoff_digest = _digest(FRAUD_HEADLINE, known_at=PUBLISHED, first_seen_at=LATE_SEEN)
    late_cutoff_digest = _digest(FRAUD_HEADLINE, known_at=LATE_SEEN, first_seen_at=LATE_SEEN)

    early = _only(rank_watchlist(early_cutoff_digest, {}), "SBIN")
    late = _only(rank_watchlist(late_cutoff_digest, {}), "SBIN")

    assert early.reasons == ()
    assert early.score == 0
    assert late.score > 0
    assert late.reasons != ()


# --------------------------------------------------------------------------- #
# Market context: absence is never fabricated as zero
# --------------------------------------------------------------------------- #


def test_a_missing_market_context_is_flagged_not_silently_scored_as_flat() -> None:
    """No context requested at all is not the same fact as a flat, unchanged close."""
    digest = _digest()
    ranked = rank_watchlist(digest, {})

    entry = _only(ranked, "SBIN")
    assert entry.market_context_available is False
    assert entry.score == 0


def test_a_no_data_market_context_is_flagged_not_silently_scored_as_flat() -> None:
    """NO_DATA is DHRUVA not knowing, never a computed zero-movement answer."""
    digest = _digest()
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, availability=MarketDataAvailability.NO_DATA)},
    )

    entry = _only(ranked, "SBIN")
    assert entry.market_context_available is False


def test_available_market_context_with_no_movement_is_distinct_from_absent() -> None:
    """A present, computed 0% move is a real fact; an absent context is not."""
    digest = _digest()
    ranked = rank_watchlist(digest, {SBIN.instrument_id: _context(SBIN, one_day="0.00")})

    entry = _only(ranked, "SBIN")
    assert entry.market_context_available is True
    assert entry.score == 0


def test_insufficient_history_leaves_price_points_unscored_without_a_crash() -> None:
    """One bar has a close but no comparison; there is nothing wrong here to flag."""
    digest = _digest()
    insufficient = MarketDataAvailability.INSUFFICIENT_HISTORY
    ranked = rank_watchlist(
        digest,
        {SBIN.instrument_id: _context(SBIN, availability=insufficient)},
    )

    entry = _only(ranked, "SBIN")
    assert entry.market_context_available is True
    assert entry.score == 0


# --------------------------------------------------------------------------- #
# Bounds, invariants and independence of components
# --------------------------------------------------------------------------- #


def test_the_score_is_always_within_its_documented_bound() -> None:
    """Even a maximally eventful instrument cannot exceed MAX_ATTENTION_SCORE."""
    digest = _digest(*([FRAUD_HEADLINE] * 5))
    ranked = rank_watchlist(
        digest,
        {
            SBIN.instrument_id: _context(
                SBIN,
                one_day="20.00",
                multi_day="20.00",
                sessions=5,
                volume_ratio="10.00",
                baseline_sessions=20,
            )
        },
    )

    entry = _only(ranked, "SBIN")
    assert entry.score <= MAX_ATTENTION_SCORE


def test_a_band_that_disagrees_with_its_own_score_is_refused() -> None:
    """The invariant a caller could otherwise violate by constructing one by hand."""
    with pytest.raises(InvariantViolation, match="does not match"):
        ResearchAttention(
            instrument_id=SBIN.instrument_id,
            canonical_symbol="SBIN",
            company_name="State Bank of India",
            as_of=ANALYSED,
            score=0,
            band=AttentionBand.HIGH,
            reasons=(),
            market_context_available=False,
        )


def test_an_out_of_bounds_score_is_refused() -> None:
    """A score above the documented maximum cannot be constructed."""
    with pytest.raises(InvariantViolation, match="out of bounds"):
        ResearchAttention(
            instrument_id=SBIN.instrument_id,
            canonical_symbol="SBIN",
            company_name="State Bank of India",
            as_of=ANALYSED,
            score=MAX_ATTENTION_SCORE + 1,
            band=AttentionBand.HIGH,
            reasons=(),
            market_context_available=False,
        )


def test_an_empty_watchlist_produces_an_empty_ranking() -> None:
    """An empty watchlist is a valid, complete, empty answer -- not an error."""
    empty_digest = build_digest(
        (), (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED
    )

    assert rank_watchlist(empty_digest, {}) == ()
