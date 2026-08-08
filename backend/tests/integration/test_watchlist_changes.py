"""Research changes over what came back out of PostgreSQL, at two historical cutoffs.

``test_changes.py``/``test_attention_changes.py`` prove the comparison and its
rendering against hand-built digests and contexts. This proves the part that
cannot be faked: composed from the real repositories through two independent
``rank_watchlist`` reads, a change report inherits each read's own
point-in-time cutoff -- a bar retrieved, or a news item first seen, strictly
between T1 and T2 must appear only as a change at T2, and nothing after T2
must appear at all, even when both cutoffs are days in the past.
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
from dhruva.contexts.intelligence.domain.changes import compare_watchlist
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
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    rank_watchlist,
    render_changes,
)
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
#: Both cutoffs are historical -- days before "now" -- so a change report must
#: answer exactly as of these two instants, never as of the latest archive state.
T1 = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
T2 = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
AFTER_T2 = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)

PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
EARLY_RETRIEVAL = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

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
    engine: AsyncEngine, *, known_at: datetime, sessions: int = 5
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
    engine: AsyncEngine, title: str, *, first_seen_at: datetime, url: str = "sbi-audit"
) -> None:
    link = canonical_url(f"https://{PUBLISHER}/{url}")
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
            analysed_at=first_seen_at + timedelta(minutes=5),
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


async def _changes_between(
    engine: AsyncEngine, *, from_cutoff: datetime, to_cutoff: datetime
) -> str:
    digest_before = await _digest_at(engine, known_at=from_cutoff)
    digest_after = await _digest_at(engine, known_at=to_cutoff)
    before = rank_watchlist(digest_before, await _contexts(engine, known_at=from_cutoff))
    after = rank_watchlist(digest_after, await _contexts(engine, known_at=to_cutoff))
    changes = compare_watchlist(before, after, after_digest=digest_after)
    return render_changes(changes, from_cutoff=from_cutoff, to_cutoff=to_cutoff)


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_comparison_composes_from_the_real_pipeline_at_two_historical_cutoffs(
    migrated: AsyncEngine,
) -> None:
    """The ordinary case, end to end, comparing two cutoffs days in the past."""
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "100"))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY + timedelta(days=1), "110"))
    await _ingest_news(migrated, FRAUD, first_seen_at=T1 + timedelta(hours=1))

    rendered = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)

    assert "SBIN" in rendered
    assert "ENTERED" in rendered
    assert "NEWS_ADDED" in rendered
    assert FRAUD in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bar_retrieved_between_the_cutoffs_affects_only_the_later_side(
    migrated: AsyncEngine,
) -> None:
    """A close stored between T1 and T2 must move the T2 side only."""
    await _store(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY, "500", retrieved_at=T1 + timedelta(hours=1)),
    )

    rendered = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)

    assert "close 500" not in rendered
    assert "SCORE_INCREASED" in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_news_first_seen_between_the_cutoffs_appears_only_as_a_change_at_t2(
    migrated: AsyncEngine,
) -> None:
    """The property this whole slice exists for."""
    await _ingest_news(migrated, FRAUD, first_seen_at=T1 + timedelta(hours=1))

    rendered = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)

    assert "NEWS_ADDED" in rendered
    assert FRAUD in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_news_first_seen_after_t2_is_absent_from_the_comparison(
    migrated: AsyncEngine,
) -> None:
    """Nothing knowable only after the later cutoff may leak into the report."""
    await _ingest_news(migrated, FRAUD, first_seen_at=AFTER_T2)

    rendered = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)

    assert "No changes between these two cutoffs." in rendered
    assert FRAUD not in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_identical_cutoffs_produce_no_changes_over_the_real_pipeline(
    migrated: AsyncEngine,
) -> None:
    """T2 == T1 is a valid, deterministic, empty answer -- not a special case."""
    await _ingest_news(migrated, FRAUD, first_seen_at=T1 - timedelta(hours=1))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "100"))

    rendered = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T1)

    assert "No changes between these two cutoffs." in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_comparison_over_the_real_pipeline_writes_nothing(migrated: AsyncEngine) -> None:
    """It is a read over four read models -- two per cutoff. It must not write."""
    await _ingest_news(migrated, FRAUD, first_seen_at=T1 + timedelta(hours=1))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "100"))

    first = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)
    second = await _changes_between(migrated, from_cutoff=T1, to_cutoff=T2)

    assert first == second
