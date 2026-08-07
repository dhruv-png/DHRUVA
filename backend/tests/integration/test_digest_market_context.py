"""Market context in the digest, over bars that came back out of PostgreSQL.

The unit suite proves the arithmetic. This proves the part that cannot be faked:
that the ``known_at`` cutoff reaches the daily-bar repository, that a bar stored
*after* the cutoff cannot influence a summary computed *at* it, and that the
news read and the bar read agree about which instant they were asked about.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
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
from dhruva.contexts.intelligence.interfaces.digest_presentation import render_digest
from dhruva.contexts.marketdata.api import (
    GetMarketContext,
    GetMarketContextQuery,
    MarketDataAvailability,
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
from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
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
    """Append bars through the real repository and commit."""
    factory = _market_factory(engine)
    async with factory(ACCOUNT) as unit_of_work:
        await unit_of_work.daily_bars.add_series(DailyBarSeries(bars=tuple(bars)))
        await unit_of_work.commit()


async def _contexts(
    engine: AsyncEngine,
    *,
    known_at: datetime = KNOWN_AT,
    sessions: int = 5,
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


async def _ingest_news(engine: AsyncEngine, title: str) -> None:
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
        first_seen_at=FIRST_SEEN,
    )
    await IngestNewsItems(_news_factory(engine)).execute(
        IngestNewsItemsCommand(
            account_id=ACCOUNT, items=(item,), universe=UNIVERSE, analysed_at=ANALYSED
        )
    )


async def _count_bars(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            select(func.count()).select_from(DailyMarketBarRevisionModel)
        )
        return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Normal context
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_stored_bars_produce_a_usable_summary(migrated: AsyncEngine) -> None:
    """The ordinary case, end to end through the real repository."""
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )

    context = (await _contexts(migrated))[SBIN.instrument_id]

    assert context.availability is MarketDataAvailability.AVAILABLE
    assert context.latest_close == Decimal("110")
    assert context.one_day_change_percent == Decimal("10.00")


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_multi_day_return_uses_the_bar_that_many_sessions_back(
    migrated: AsyncEngine,
) -> None:
    """Six stored bars, five sessions: the arithmetic spans the whole window."""
    await _store(
        migrated,
        *(_bar(SBIN, CUTOFF_DAY - timedelta(days=5 - n), str(100 + n)) for n in range(6)),
    )

    context = (await _contexts(migrated))[SBIN.instrument_id]

    assert context.multi_day_sessions == 5
    assert context.multi_day_change_percent == Decimal("5.00")


@pytest.mark.usefixtures("truncated_after_test")
async def test_volume_is_compared_against_the_stored_prior_sessions(
    migrated: AsyncEngine,
) -> None:
    """The comparison reads bars, not an assumption about typical volume."""
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100", volume=1_000),
        _bar(SBIN, CUTOFF_DAY, "110", volume=3_000),
    )

    context = (await _contexts(migrated))[SBIN.instrument_id]

    assert context.latest_volume == 3_000
    assert context.volume_ratio == Decimal("3.00")


# --------------------------------------------------------------------------- #
# Absence, thin history, staleness
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_instrument_with_no_bars_returns_a_reported_absence(
    migrated: AsyncEngine,
) -> None:
    """The repository raises; assembling a report over twenty instruments must not."""
    contexts = await _contexts(migrated)

    assert contexts[SBIN.instrument_id].availability is MarketDataAvailability.NO_DATA
    assert contexts[SBIN.instrument_id].limitation is not None
    assert len(contexts) == len(UNIVERSE)


@pytest.mark.usefixtures("truncated_after_test")
async def test_one_stored_bar_yields_a_close_and_no_comparison(
    migrated: AsyncEngine,
) -> None:
    """A close with nothing to compare it against is exactly that."""
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "110"))

    context = (await _contexts(migrated))[SBIN.instrument_id]

    assert context.availability is MarketDataAvailability.INSUFFICIENT_HISTORY
    assert context.latest_close == Decimal("110")
    assert context.one_day_change_percent is None


@pytest.mark.usefixtures("truncated_after_test")
async def test_bars_older_than_the_bound_are_reported_as_stale(
    migrated: AsyncEngine,
) -> None:
    """Presenting an old close as today's is the failure that matters most."""
    old = CUTOFF_DAY - timedelta(days=20)
    await _store(
        migrated,
        _bar(SBIN, old - timedelta(days=1), "100"),
        _bar(SBIN, old, "110"),
    )

    context = (await _contexts(migrated))[SBIN.instrument_id]

    assert context.is_stale is True
    assert context.latest_date == old
    assert context.staleness_days == 20


# --------------------------------------------------------------------------- #
# The cutoff is absolute
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bar_retrieved_after_the_cutoff_cannot_influence_the_summary(
    migrated: AsyncEngine,
) -> None:
    """The property the whole bitemporal schema exists for.

    Both bars are dated on or before the cutoff *day*; one was retrieved the
    following day. A summary computed at the earlier instant must not see it,
    or a backtest reading this digest would be trading on tomorrow's tape.
    """
    await _store(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "500", retrieved_at=LATE_RETRIEVAL))

    at_cutoff = (await _contexts(migrated))[SBIN.instrument_id]
    later = (await _contexts(migrated, known_at=LATE_RETRIEVAL + timedelta(hours=1)))[
        SBIN.instrument_id
    ]

    assert at_cutoff.latest_close == Decimal("100")
    assert at_cutoff.availability is MarketDataAvailability.INSUFFICIENT_HISTORY
    assert later.latest_close == Decimal("500")


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_correction_retrieved_later_does_not_rewrite_the_earlier_answer(
    migrated: AsyncEngine,
) -> None:
    """The same trading date, restated. The earlier read keeps the earlier close."""
    await _store(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "110"))
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "115", retrieved_at=LATE_RETRIEVAL))

    at_cutoff = (await _contexts(migrated))[SBIN.instrument_id]
    later = (await _contexts(migrated, known_at=LATE_RETRIEVAL + timedelta(hours=1)))[
        SBIN.instrument_id
    ]

    assert at_cutoff.latest_close == Decimal("110")
    assert later.latest_close == Decimal("115")


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_cutoff_before_every_stored_bar_reports_no_data(
    migrated: AsyncEngine,
) -> None:
    """Not zero, not flat -- nothing was knowable yet."""
    await _store(migrated, _bar(SBIN, CUTOFF_DAY, "110"))

    context = (await _contexts(migrated, known_at=EARLY_RETRIEVAL - timedelta(days=1)))[
        SBIN.instrument_id
    ]

    assert context.availability is MarketDataAvailability.NO_DATA


# --------------------------------------------------------------------------- #
# Composition with the news digest
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_news_and_market_context_appear_together_for_one_instrument(
    migrated: AsyncEngine,
) -> None:
    """The product question: what changed, and what did the price do."""
    await _ingest_news(migrated, FRAUD)
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "90"),
    )

    digest = await BuildWatchlistDigest(_news_factory(migrated)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=KNOWN_AT,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=KNOWN_AT,
        )
    )
    rendered = render_digest(digest, await _contexts(migrated))

    assert "FRAUD_GOVERNANCE" in rendered
    assert "close 90" in rendered
    assert "-10.00%" in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_market_context_appears_for_an_instrument_with_no_news(
    migrated: AsyncEngine,
) -> None:
    """A quiet instrument that moved is still worth showing."""
    await _store(
        migrated,
        _bar(HAL, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(HAL, CUTOFF_DAY, "104"),
    )

    digest = await BuildWatchlistDigest(_news_factory(migrated)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=KNOWN_AT,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=KNOWN_AT,
        )
    )
    rendered = render_digest(digest, await _contexts(migrated))

    assert "nothing archived in this window" in rendered
    assert "close 104" in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_news_with_no_market_data_still_renders_its_news(
    migrated: AsyncEngine,
) -> None:
    """One archive being empty must not suppress the other."""
    await _ingest_news(migrated, FRAUD)

    digest = await BuildWatchlistDigest(_news_factory(migrated)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=KNOWN_AT,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=KNOWN_AT,
        )
    )
    rendered = render_digest(digest, await _contexts(migrated))

    assert "FRAUD_GOVERNANCE" in rendered
    assert "no data" in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_several_instruments_each_get_their_own_context(
    migrated: AsyncEngine,
) -> None:
    """One summary per instrument, and the summaries do not bleed into each other."""
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )
    await _store(migrated, _bar(HAL, CUTOFF_DAY, "50"))

    contexts = await _contexts(migrated)

    assert contexts[SBIN.instrument_id].latest_close == Decimal("110")
    assert contexts[HAL.instrument_id].latest_close == Decimal("50")
    assert contexts[HAL.instrument_id].availability is (MarketDataAvailability.INSUFFICIENT_HISTORY)


# --------------------------------------------------------------------------- #
# The read path stays a read path
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_reading_market_context_writes_nothing(migrated: AsyncEngine) -> None:
    """It is a query. Repeating it must not change the archive or the answer."""
    await _store(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )
    before = await _count_bars(migrated)

    first = await _contexts(migrated)
    second = await _contexts(migrated)

    assert first == second
    assert await _count_bars(migrated) == before


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_summary_is_deterministic_across_repeated_reads(
    migrated: AsyncEngine,
) -> None:
    """Arithmetic over stored values must not depend on row order."""
    await _store(
        migrated,
        *(_bar(SBIN, CUTOFF_DAY - timedelta(days=9 - n), str(100 + n)) for n in range(10)),
    )

    answers = [(await _contexts(migrated))[SBIN.instrument_id] for _ in range(3)]

    assert answers[0] == answers[1] == answers[2]
