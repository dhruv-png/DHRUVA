"""Historical planning and chunk ingestion stay bounded, restartable and PIT-safe."""

# ruff: noqa: D101, D102, D103, D105, PLR0913 - terse test doubles and test names are explicit

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.marketdata.application.daily_history import (
    IngestHistoricalDailyHistory,
    IngestHistoricalDailyHistoryCommand,
)
from dhruva.contexts.marketdata.application.historical_backfill import (
    BACKFILL_CHUNK_CALENDAR_DAYS,
    BACKFILL_OVERLAP_CALENDAR_DAYS,
    BackfillInstrument,
    BackfillUniverseRole,
    BenchmarkReturnBasis,
    HistoricalBackfillPlan,
    plan_historical_backfill,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyBarArchiveWrite,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.shared.errors import DataQualityError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import marketdata as cli

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")
STOCK = InstrumentId.deterministic("reference", "nse-equity-test")
RETRIEVED = datetime(2026, 8, 9, 8, tzinfo=UTC)


def _instrument(
    instrument_id: InstrumentId,
    symbol: str,
    kind: MarketInstrumentKind,
    role: BackfillUniverseRole,
    *,
    earliest: date | None,
    token: int,
) -> BackfillInstrument:
    return BackfillInstrument(
        instrument_id=instrument_id,
        canonical_symbol=symbol,
        instrument_kind=kind,
        source_instrument_id=token,
        role=role,
        earliest_stored=earliest,
    )


def _benchmark(*, earliest: date | None = None) -> BackfillInstrument:
    return _instrument(
        BENCHMARK,
        "NIFTY 50",
        MarketInstrumentKind.INDEX,
        BackfillUniverseRole.BENCHMARK,
        earliest=earliest,
        token=100,
    )


def test_planning_is_deterministic_bounded_and_truthfully_price_index() -> None:
    stock_z = _instrument(
        InstrumentId.deterministic("reference", "z"),
        "ZZZ",
        MarketInstrumentKind.CASH_EQUITY,
        BackfillUniverseRole.OWNER_WATCHLIST,
        earliest=date(2026, 7, 1),
        token=300,
    )
    stock_a = _instrument(
        InstrumentId.deterministic("reference", "a"),
        "AAA",
        MarketInstrumentKind.CASH_EQUITY,
        BackfillUniverseRole.OWNER_WATCHLIST,
        earliest=date(2026, 7, 1),
        token=200,
    )

    def build_plan() -> HistoricalBackfillPlan:
        return plan_historical_backfill(
            target_from=date(2024, 1, 1),
            completed_through=date(2026, 8, 8),
            benchmark=_benchmark(earliest=date(2026, 7, 1)),
            benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
            instruments=(stock_z, stock_a),
        )

    first = build_plan()
    second = build_plan()

    assert first == second
    assert first.benchmark_basis is BenchmarkReturnBasis.PRICE_INDEX
    assert [item.target_symbol for item in first.chunks[:3]] == [
        "NIFTY 50",
        "NIFTY 50",
        "NIFTY 50",
    ]
    assert (
        next(
            item.target_symbol
            for item in first.chunks
            if item.role is not BackfillUniverseRole.BENCHMARK
        )
        == "AAA"
    )
    assert all(
        (item.request_to - item.request_from).days
        <= BACKFILL_CHUNK_CALENDAR_DAYS + BACKFILL_OVERLAP_CALENDAR_DAYS - 1
        for item in first.chunks
    )
    assert first.provider_requests == sum(len(item.requests) for item in first.chunks)


def test_resume_plan_uses_the_current_earliest_stored_frontier() -> None:
    complete = _benchmark(earliest=date(2024, 1, 1))

    plan = plan_historical_backfill(
        target_from=date(2024, 1, 1),
        completed_through=date(2026, 8, 8),
        benchmark=complete,
        benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
        instruments=(),
    )

    assert plan.chunks == ()
    assert plan.provider_requests == 0


def test_more_than_ten_years_is_refused_before_a_request_exists() -> None:
    with pytest.raises(ValidationError, match="ten-year safety bound"):
        plan_historical_backfill(
            target_from=date(2010, 1, 1),
            completed_through=date(2026, 8, 8),
            benchmark=_benchmark(),
            benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
            instruments=(),
        )


def _candle(item: date) -> DailyCandle:
    return DailyCandle(
        trading_date=item,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=1000,
        open_interest=None,
    )


def _batch(
    request: DailyHistoryRequest,
    *,
    dates: tuple[date, ...],
    returned_request: DailyHistoryRequest | None = None,
) -> DailyHistoryBatch:
    raw = f"{request.instrument_id}-{request.from_date}-{dates}".encode()
    return DailyHistoryBatch(
        provider="zerodha",
        request=request if returned_request is None else returned_request,
        retrieved_at=RETRIEVED,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        raw_response=raw,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        candles=tuple(_candle(item) for item in dates),
    )


class FakeSource:
    def __init__(
        self, dates: dict[InstrumentId, tuple[date, ...]], *, fail_after: int | None = None
    ) -> None:
        self.dates = dates
        self.fail_after = fail_after
        self.requests: list[DailyHistoryRequest] = []

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        self.requests.append(request)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise DataQualityError("arranged later-chunk failure")
        dates = tuple(
            item
            for item in self.dates[request.instrument_id]
            if request.from_date <= item <= request.to_date
        )
        return _batch(request, dates=dates)


class MismatchedSource:
    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        returned = replace(
            request,
            instrument_id=InstrumentId.deterministic("reference", "wrong-instrument"),
        )
        return _batch(request, dates=(request.from_date,), returned_request=returned)


class FakeStore:
    def __init__(self) -> None:
        self.series: list[DailyBarSeries] = []
        self.revisions: set[tuple[InstrumentId, str]] = set()

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        self.series.append(series)
        added = 0
        for bar in series.bars:
            key = (bar.instrument_id, bar.source_revision)
            if key not in self.revisions:
                self.revisions.add(key)
                added += 1
        return DailyBarArchiveWrite(added=added, unchanged=len(series.bars) - added)

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,
    ) -> DailyBarSeries:
        raise AssertionError(instrument_id, from_date, to_date, known_at, require_complete)


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self.daily_bars = store
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return


class Factory:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.units: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        assert account_id == ACCOUNT
        unit = FakeUnitOfWork(self.store)
        self.units.append(unit)
        return unit


def _requests(dates: tuple[date, ...]) -> tuple[DailyHistoryRequest, ...]:
    return (
        DailyHistoryRequest(
            instrument_id=BENCHMARK,
            instrument_kind=MarketInstrumentKind.INDEX,
            source_instrument_id=100,
            from_date=dates[0],
            to_date=dates[-1],
        ),
        DailyHistoryRequest(
            instrument_id=STOCK,
            instrument_kind=MarketInstrumentKind.CASH_EQUITY,
            source_instrument_id=200,
            from_date=dates[0],
            to_date=dates[-1],
        ),
    )


@pytest.mark.asyncio
async def test_chunk_retry_is_idempotent_and_keeps_modern_knowledge_time() -> None:
    dates = (date(2020, 1, 2), date(2020, 1, 3))
    source = FakeSource({BENCHMARK: dates, STOCK: dates})
    store = FakeStore()
    factory = Factory(store)
    service = IngestHistoricalDailyHistory(source, factory)
    command = IngestHistoricalDailyHistoryCommand(
        account_id=ACCOUNT,
        requests=_requests(dates),
        benchmark_id=BENCHMARK,
        completed_through=date(2026, 8, 8),
    )

    first = await service.execute(command)
    second = await service.execute(command)

    assert (first.bars_added, second.bars_added, second.bars_unchanged) == (4, 0, 4)
    assert all(
        bar.retrieved_at == RETRIEVED and bar.candle.trading_date.year == 2020
        for series in store.series
        for bar in series.bars
    )


@pytest.mark.asyncio
async def test_a_historical_stock_must_match_benchmark_sessions() -> None:
    dates = (date(2020, 1, 2), date(2020, 1, 3))
    source = FakeSource({BENCHMARK: dates, STOCK: dates[:1]})
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="does not match benchmark sessions"):
        await IngestHistoricalDailyHistory(source, factory).execute(
            IngestHistoricalDailyHistoryCommand(
                account_id=ACCOUNT,
                requests=_requests(dates),
                benchmark_id=BENCHMARK,
                completed_through=date(2026, 8, 8),
            )
        )

    assert factory.units == []


@pytest.mark.asyncio
async def test_malformed_provider_identity_fails_before_any_transaction() -> None:
    dates = (date(2020, 1, 2), date(2020, 1, 3))
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="different instrument request"):
        await IngestHistoricalDailyHistory(MismatchedSource(), factory).execute(
            IngestHistoricalDailyHistoryCommand(
                account_id=ACCOUNT,
                requests=_requests(dates),
                benchmark_id=BENCHMARK,
                completed_through=date(2026, 8, 8),
            )
        )

    assert factory.units == []


@pytest.mark.asyncio
async def test_later_chunk_failure_keeps_earlier_commit_and_exact_call_count() -> None:
    plan = plan_historical_backfill(
        target_from=date(2025, 1, 1),
        completed_through=date(2026, 8, 8),
        benchmark=_benchmark(),
        benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
        instruments=(),
    )
    source = FakeSource({BENCHMARK: (date(2025, 7, 1), date(2026, 8, 1))}, fail_after=1)
    store = FakeStore()

    result = await cli._execute_backfill(plan, ACCOUNT, source, Factory(store))

    assert result == 2
    assert len(source.requests) == 2
    assert len(store.series) == 1
    assert len(source.requests) <= plan.provider_requests


@pytest.mark.asyncio
async def test_success_issues_exactly_the_planned_sequential_requests() -> None:
    plan = plan_historical_backfill(
        target_from=date(2026, 1, 1),
        completed_through=date(2026, 8, 8),
        benchmark=_benchmark(),
        benchmark_basis=BenchmarkReturnBasis.PRICE_INDEX,
        instruments=(),
    )
    source = FakeSource({BENCHMARK: (date(2026, 1, 2),)})

    result = await cli._execute_backfill(plan, ACCOUNT, source, Factory(FakeStore()))

    assert result == 0
    assert len(source.requests) == plan.provider_requests == 1
