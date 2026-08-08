"""Research attention over what came back out of PostgreSQL, not a fixture.

``test_attention.py`` proves the arithmetic against hand-built digests and
contexts. This proves the part that cannot be faked: that ``rank_watchlist``
composed from the real repositories inherits their point-in-time cutoff --
a bar or a news item stored after the cutoff must not move a score computed
at it, exactly as it must not move a rendered digest.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.news import (
    NewsItem,
    NewsItemIdentity,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_SOURCE_KEY,
    gdelt_source,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.attention_presentation import rank_watchlist
from dhruva.contexts.marketdata.api import (
    GetMarketContext,
    GetMarketContextQuery,
    contexts_by_instrument,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dhruva.contexts.marketdata.domain.market_context import MarketContext

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
CUTOFF_DAY = date(2026, 8, 3)
KNOWN_AT = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
EARLY_RETRIEVAL = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
LATE_RETRIEVAL = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
FIRST_SEEN = PUBLISHED + timedelta(minutes=30)
LATE_FIRST_SEEN = datetime(2026, 8, 4, 6, 0, tzinfo=UTC)
ANALYSED = FIRST_SEEN + timedelta(minutes=5)

FRAUD = "State Bank of India faces a forensic audit over accounting irregularities"

SBIN = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-sbin"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
HAL = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
)
UNIVERSE = (SBIN, HAL)
PUBLISHER = "economictimes.indiatimes.test"


def _market_factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyMarketDataUnitOfWork(sessions, account_id=account_id)


def _news_factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyIntelligenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


def _bar(
    instrument: LinkableInstrument,
    trading_date: date,
    close: str,
    *,
    retrieved_at: datetime = EARLY_RETRIEVAL,
    volume: int = 1_000,
) -> DailyBarRevision:
    price = Decimal(close)
    return DailyBarRevision(
        instrument_id=instrument.instrument_id,
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
        retrieved_at=retrieved_at,
        adjustment_status=AdjustmentStatus.RAW,
        completeness=BarCompleteness.COMPLETE,
        source_revision=hashlib.sha256(
            f"{instrument.canonical_symbol}{trading_date}{close}{retrieved_at}".encode()
        ).hexdigest(),
        batch_sha256="b" * 64,
        quality_revision="daily-bar-quality-v1",
    )


async def _store(engine: AsyncEngine, *bars: DailyBarRevision) -> None:
    factory = _market_factory(engine)
    async with factory(ACCOUNT) as unit_of_work:
        await unit_of_work.daily_bars.add_series(DailyBarSeries(bars=tuple(bars)))
        await unit_of_work.commit()


async def _contexts(
    engine: AsyncEngine, *, known_at: datetime = KNOWN_AT, sessions: int = 5
) -> dict[InstrumentId, MarketContext]:
    summaries = await GetMarketContext(_market_factory(engine)).execute(
        GetMarketContextQuery(
            account_id=ACCOUNT,
            instrument_ids=tuple(entry.instrument_id for entry in UNIVERSE),
            known_at=known_at,
            sessions=sessions,
        )
    )
    return contexts_by_instrument(summaries)


async def _ingest_news(
    engine: AsyncEngine, title: str, *, first_seen_at: datetime = FIRST_SEEN
) -> None:
    link = canonical_url(f"https://{PUBLISHER}/sbi-audit")
    item = NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT_SOURCE_KEY,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=gdelt_source(PUBLISHER),
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=first_seen_at,
    )
    await IngestNewsItems(_news_factory(engine)).execute(
        IngestNewsItemsCommand(
            account_id=ACCOUNT,
            items=(item,),
            universe=UNIVERSE,
            analysed_at=max(ANALYSED, first_seen_at + timedelta(minutes=5)),
        )
    )


async def _digest_at(engine: AsyncEngine, *, known_at: datetime):  # type: ignore[no-untyped-def]
    return await BuildWatchlistDigest(_news_factory(engine)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=known_at,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=known_at,
        )
    )


@pytest.mark.usefixtures("truncated_after_test")
async def test_attention_composes_from_the_real_digest_and_market_context(
    migrated: AsyncEngine,
) -> None:
    """The ordinary case, end to end through both real repositories."""
    await _ingest_news(migrated, FRAUD)
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "90"),
    )

    digest = await _digest_at(migrated, known_at=KNOWN_AT)
    ranked = rank_watchlist(digest, await _contexts(migrated))

    sbin = next(entry for entry in ranked if entry.canonical_symbol == "SBIN")
    assert sbin.market_context_available is True
    assert any("move" in reason for reason in sbin.reasons)
    assert any("news" in reason for reason in sbin.reasons)
    assert sbin.band is not AttentionBand.LOW


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bar_retrieved_after_the_cutoff_cannot_move_the_score(
    migrated: AsyncEngine,
) -> None:
    """The bitemporal property, carried through into the score.

    A close stored *after* the cutoff must not move a score computed *at* it --
    the same guarantee ``test_digest_market_context.py`` proves for the
    rendered digest, proven here one layer further downstream.
    """
    await _store(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "500", retrieved_at=LATE_RETRIEVAL))

    at_cutoff = rank_watchlist(
        await _digest_at(migrated, known_at=KNOWN_AT), await _contexts(migrated)
    )
    later_known_at = LATE_RETRIEVAL + timedelta(hours=1)
    later = rank_watchlist(
        await _digest_at(migrated, known_at=later_known_at),
        await _contexts(migrated, known_at=later_known_at),
    )

    at_cutoff_sbin = next(entry for entry in at_cutoff if entry.canonical_symbol == "SBIN")
    later_sbin = next(entry for entry in later if entry.canonical_symbol == "SBIN")

    assert at_cutoff_sbin.score == 0
    assert later_sbin.score > 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_news_item_first_seen_after_the_cutoff_cannot_move_the_score(
    migrated: AsyncEngine,
) -> None:
    """The same PIT rule, on the news archive rather than the bar archive."""
    await _ingest_news(migrated, FRAUD, first_seen_at=LATE_FIRST_SEEN)

    at_cutoff = rank_watchlist(
        await _digest_at(migrated, known_at=KNOWN_AT), await _contexts(migrated)
    )
    later_known_at = LATE_FIRST_SEEN + timedelta(hours=1)
    later = rank_watchlist(
        await _digest_at(migrated, known_at=later_known_at),
        await _contexts(migrated, known_at=later_known_at),
    )

    at_cutoff_sbin = next(entry for entry in at_cutoff if entry.canonical_symbol == "SBIN")
    later_sbin = next(entry for entry in later if entry.canonical_symbol == "SBIN")

    assert at_cutoff_sbin.score == 0
    assert at_cutoff_sbin.reasons == ()
    assert later_sbin.score > 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_instrument_with_no_stored_bars_is_marked_unavailable_not_zero(
    migrated: AsyncEngine,
) -> None:
    """No data is a different fact from a flat close, all the way to the score."""
    ranked = rank_watchlist(
        await _digest_at(migrated, known_at=KNOWN_AT), await _contexts(migrated)
    )

    for entry in ranked:
        assert entry.market_context_available is False


@pytest.mark.usefixtures("truncated_after_test")
async def test_ranking_the_real_pipeline_writes_nothing(migrated: AsyncEngine) -> None:
    """It is a read over two read models. Repeating it must not change either."""
    await _ingest_news(migrated, FRAUD)
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "90"),
    )

    digest = await _digest_at(migrated, known_at=KNOWN_AT)
    contexts = await _contexts(migrated)

    first = rank_watchlist(digest, contexts)
    second = rank_watchlist(digest, contexts)

    assert first == second
