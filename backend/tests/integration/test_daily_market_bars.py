"""Point-in-time daily bar persistence against real PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.marketdata.application.daily_history import (
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
    IngestDailyHistory,
    IngestDailyHistoryCommand,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.shared.errors import DataQualityError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")
STOCK = InstrumentId.deterministic("reference", "nse-equity-test")
DATES = (date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31))
FIRST_RETRIEVED = datetime(2026, 8, 1, 18, tzinfo=UTC)
CORRECTED_AT = FIRST_RETRIEVED + timedelta(hours=2)


class FakeSource:
    """Return deterministic batches by stable instrument identity."""

    def __init__(self, batches: tuple[DailyHistoryBatch, ...]) -> None:
        self.batches = {batch.request.instrument_id: batch for batch in batches}

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Return the arranged stable-identity batch."""
        return self.batches[request.instrument_id]


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork]:
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

    def create(account_id: AccountId) -> SqlAlchemyMarketDataUnitOfWork:
        return SqlAlchemyMarketDataUnitOfWork(sessions, account_id=account_id)

    return create


def _request(
    instrument_id: InstrumentId,
    token: int,
    kind: MarketInstrumentKind,
) -> DailyHistoryRequest:
    return DailyHistoryRequest(
        instrument_id=instrument_id,
        instrument_kind=kind,
        source_instrument_id=token,
        from_date=DATES[0],
        to_date=DATES[-1],
    )


REQUESTS = (
    _request(BENCHMARK, 100, MarketInstrumentKind.INDEX),
    _request(STOCK, 200, MarketInstrumentKind.CASH_EQUITY),
)


def _candle(trading_date: date, *, close: Decimal = Decimal("103")) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=Decimal("100"),
        high=max(Decimal("105"), close),
        low=Decimal("99"),
        close=close,
        volume=1000,
        open_interest=None,
    )


def _batch(
    request: DailyHistoryRequest,
    *,
    retrieved_at: datetime = FIRST_RETRIEVED,
    corrected_close: Decimal | None = None,
) -> DailyHistoryBatch:
    candles = tuple(
        _candle(item, close=corrected_close)
        if item == DATES[1] and corrected_close is not None
        else _candle(item)
        for item in DATES
    )
    raw = (f"fixture-{request.instrument_id}-{retrieved_at.isoformat()}-{corrected_close}").encode()
    return DailyHistoryBatch(
        provider="zerodha",
        request=request,
        retrieved_at=retrieved_at,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        raw_response=raw,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        candles=candles,
    )


def _command() -> IngestDailyHistoryCommand:
    return IngestDailyHistoryCommand(
        account_id=ACCOUNT,
        requests=REQUESTS,
        benchmark_id=BENCHMARK,
        completed_through=DATES[-1],
        required_through=DATES[-1],
    )


async def _count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(select(func.count()).select_from(DailyMarketBarRevisionModel))
            or 0
        )


async def test_daily_bars_are_idempotent_and_queries_are_point_in_time(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """A correction adds one revision and remains invisible before retrieval."""
    factory = _factory(migrated)
    first_source = FakeSource(tuple(_batch(request) for request in REQUESTS))
    first = await IngestDailyHistory(first_source, factory).execute(_command())
    retry_source = FakeSource(
        tuple(
            _batch(request, retrieved_at=FIRST_RETRIEVED + timedelta(hours=1))
            for request in REQUESTS
        )
    )
    retry = await IngestDailyHistory(retry_source, factory).execute(_command())
    corrected_source = FakeSource(
        (
            _batch(REQUESTS[0], retrieved_at=CORRECTED_AT),
            _batch(
                REQUESTS[1],
                retrieved_at=CORRECTED_AT,
                corrected_close=Decimal("104"),
            ),
        )
    )
    corrected = await IngestDailyHistory(corrected_source, factory).execute(_command())

    before = await GetDailyBarSeries(factory).execute(
        GetDailyBarSeriesQuery(
            account_id=ACCOUNT,
            instrument_id=STOCK,
            from_date=DATES[0],
            to_date=DATES[-1],
            known_at=FIRST_RETRIEVED,
        )
    )
    after = await GetDailyBarSeries(factory).execute(
        GetDailyBarSeriesQuery(
            account_id=ACCOUNT,
            instrument_id=STOCK,
            from_date=DATES[0],
            to_date=DATES[-1],
            known_at=CORRECTED_AT,
        )
    )

    assert first.bars_added == 6
    assert retry.bars_added == 0
    assert retry.bars_unchanged == 6
    assert corrected.bars_added == 1
    assert corrected.bars_unchanged == 5
    assert await _count(migrated) == 7
    assert before.bars[1].candle.close == Decimal("103")
    assert after.bars[1].candle.close == Decimal("104")
    assert all(item.adjustment_status is AdjustmentStatus.UNKNOWN for item in after.bars)


async def test_incomplete_latest_bar_is_persisted_but_refused_by_complete_query(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """An unclosed latest session stays inspectable but cannot feed a complete series."""
    factory = _factory(migrated)
    command = replace(
        _command(),
        completed_through=DATES[1],
        required_through=DATES[1],
    )
    await IngestDailyHistory(
        FakeSource(tuple(_batch(request) for request in REQUESTS)),
        factory,
    ).execute(command)
    query = GetDailyBarSeriesQuery(
        account_id=ACCOUNT,
        instrument_id=STOCK,
        from_date=DATES[0],
        to_date=DATES[-1],
        known_at=FIRST_RETRIEVED,
    )

    with pytest.raises(DataQualityError, match="incomplete"):
        await GetDailyBarSeries(factory).execute(query)
    visible = await GetDailyBarSeries(factory).execute(replace(query, require_complete=False))

    assert visible.bars[-1].completeness.value == "INCOMPLETE"


async def test_market_data_unit_of_work_rolls_back_by_default(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """Leaving before commit discards every primitive bulk insert."""
    factory = _factory(migrated)
    source = FakeSource(tuple(_batch(request) for request in REQUESTS))
    fetched = await source.fetch(REQUESTS[0])
    command = replace(_command(), requests=(REQUESTS[0],), benchmark_id=BENCHMARK)
    one = await IngestDailyHistory(source, factory).execute(command)
    assert one.bars_added == 3
    async with migrated.begin() as connection:
        await connection.execute(sql_text("TRUNCATE daily_market_bar_revision"))

    from dhruva.contexts.marketdata.application.daily_history import _series  # noqa: PLC0415

    async with factory(ACCOUNT) as unit_of_work:
        await unit_of_work.daily_bars.add_series(_series(fetched, completed_through=DATES[-1]))

    assert await _count(migrated) == 0


def _run_alembic(command: str, revision: str) -> None:
    """Run one migration command in Alembic's worker thread."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper
    from alembic.config import Config  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)


def _check_alembic_drift() -> None:
    """Require live schema and all three context metadata collections to agree."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper
    from alembic.config import Config  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    alembic_command.check(config)


async def test_daily_bar_migration_downgrades_reapplies_and_has_no_drift(
    migrated: AsyncEngine,
) -> None:
    """Revision 0015 is reversible and restores the sole drift-free head."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0014_instrument_archive")
    try:
        async with migrated.connect() as connection:
            remaining = await connection.scalar(
                sql_text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'daily_market_bar_revision'"
                )
            )
        assert remaining == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(sql_text("SELECT version_num FROM alembic_version"))
    assert current == "0015_daily_market_bars"
    await asyncio.to_thread(_check_alembic_drift)
