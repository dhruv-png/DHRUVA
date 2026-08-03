"""Daily history ingestion validates synchronized sessions before persistence."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.marketdata.application.daily_history import (
    IngestDailyHistory,
    IngestDailyHistoryCommand,
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
from dhruva.shared.errors import DataQualityError, StaleDataError
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")
STOCK = InstrumentId.deterministic("reference", "nse-equity-test")
RETRIEVED = datetime(2026, 8, 2, 6, tzinfo=UTC)
DATES = (date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31))


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


def _candle(
    trading_date: date,
    *,
    close: Decimal = Decimal("103"),
) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=Decimal("100"),
        high=max(Decimal("105"), close),
        low=min(Decimal("99"), close),
        close=close,
        volume=1000,
        open_interest=None,
    )


def _batch(
    request: DailyHistoryRequest,
    *,
    dates: tuple[date, ...] = DATES,
) -> DailyHistoryBatch:
    raw = f"fixture-{request.source_instrument_id}-{dates}".encode()
    return DailyHistoryBatch(
        provider="zerodha",
        request=request,
        retrieved_at=RETRIEVED,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        raw_response=raw,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        candles=tuple(_candle(item) for item in dates),
    )


class FakeSource:
    """Return arranged batches by stable instrument identity."""

    def __init__(self, batches: tuple[DailyHistoryBatch, ...]) -> None:
        self.batches = {item.request.instrument_id: item for item in batches}

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Return the batch assigned to the requested stable identity."""
        return self.batches[request.instrument_id]


class FakeStore:
    """Capture series writes and implement source-revision idempotency."""

    def __init__(self) -> None:
        self.series: list[DailyBarSeries] = []
        self.revisions: set[tuple[InstrumentId, str]] = set()

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Record new bar revisions without a database."""
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
        """Fail because ingestion tests must never execute a query."""
        raise AssertionError((instrument_id, from_date, to_date, known_at, require_complete))


class FakeUnitOfWork:
    """Expose one in-memory store and transaction counters."""

    def __init__(self, store: FakeStore) -> None:
        self.daily_bars = store
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record rollback-by-default behavior."""
        if not self.commits:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1


class Factory:
    """Retain created transactions so validation-before-write is observable."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction for the arranged account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _command() -> IngestDailyHistoryCommand:
    return IngestDailyHistoryCommand(
        account_id=ACCOUNT,
        requests=REQUESTS,
        benchmark_id=BENCHMARK,
        completed_through=DATES[1],
        required_through=DATES[1],
    )


async def test_synchronized_history_commits_complete_and_incomplete_bars() -> None:
    """The latest unclosed date remains explicit and cannot masquerade as complete."""
    store = FakeStore()
    factory = Factory(store)
    source = FakeSource(tuple(_batch(request) for request in REQUESTS))

    result = await IngestDailyHistory(source, factory).execute(_command())

    assert result.instruments == 2
    assert result.bars_added == 6
    assert result.incomplete_bars == 2
    assert result.quality_revision == "daily-history-quality-v1"
    assert factory.created[0].commits == 1
    assert all(item.adjustment_status is AdjustmentStatus.UNKNOWN for item in store.series[0].bars)


async def test_missing_benchmark_session_refuses_every_write() -> None:
    """A stock gap is not silently accepted when the benchmark proves a session existed."""
    source = FakeSource(
        (
            _batch(REQUESTS[0]),
            _batch(REQUESTS[1], dates=(DATES[0], DATES[2])),
        )
    )
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="benchmark sessions"):
        await IngestDailyHistory(source, factory).execute(
            replace(_command(), completed_through=DATES[2], required_through=DATES[2])
        )

    assert factory.created == []


async def test_missing_required_session_is_stale_not_an_empty_success() -> None:
    """The requested freshness date must exist for every instrument."""
    source = FakeSource(
        (
            _batch(REQUESTS[0]),
            _batch(REQUESTS[1], dates=DATES[:2]),
        )
    )
    factory = Factory(FakeStore())

    with pytest.raises(StaleDataError, match="stale or incomplete"):
        await IngestDailyHistory(source, factory).execute(
            replace(_command(), completed_through=DATES[2], required_through=DATES[2])
        )

    assert factory.created == []


async def test_a_batch_answering_a_different_request_is_refused() -> None:
    """A provider must never be trusted to have understood which instrument was asked for."""

    class ConfusedSource:
        """Answer every request with the benchmark batch."""

        async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
            """Ignore the requested identity, as a defective provider would."""
            assert request is not None
            return _batch(REQUESTS[0])

    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="different instrument request"):
        await IngestDailyHistory(ConfusedSource(), factory).execute(_command())

    assert factory.created == []


async def test_a_refresh_that_mixes_providers_is_refused() -> None:
    """One refresh is one provider; mixed sources cannot share a session calendar."""
    source = FakeSource((_batch(REQUESTS[0]), replace(_batch(REQUESTS[1]), provider="other")))
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="mixes providers"):
        await IngestDailyHistory(source, factory).execute(_command())

    assert factory.created == []


async def test_possible_corporate_action_discontinuity_requires_reconciliation() -> None:
    """A large unexplained close move cannot silently enter an unknown-adjustment series."""
    stock = _batch(REQUESTS[1])
    shocked = replace(
        stock,
        candles=(
            stock.candles[0],
            _candle(DATES[1], close=Decimal("50")),
            _candle(DATES[2], close=Decimal("51")),
        ),
    )
    factory = Factory(FakeStore())

    with pytest.raises(DataQualityError, match="corporate-action discontinuity"):
        await IngestDailyHistory(
            FakeSource((_batch(REQUESTS[0]), shocked)),
            factory,
        ).execute(_command())

    assert factory.created == []
