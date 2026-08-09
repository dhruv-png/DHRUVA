"""Ingest and query validated daily cash/index history."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.domain.daily_bars import (
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
)
from dhruva.shared.errors import DataQualityError, StaleDataError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from dhruva.contexts.marketdata.domain.ports import DailyHistorySource, MarketDataUnitOfWork
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "DAILY_HISTORY_QUALITY_REVISION",
    "GetDailyBarSeries",
    "GetDailyBarSeriesQuery",
    "IngestDailyHistory",
    "IngestDailyHistoryCommand",
    "IngestDailyHistoryResult",
    "IngestHistoricalDailyHistory",
    "IngestHistoricalDailyHistoryCommand",
]

DAILY_HISTORY_QUALITY_REVISION = "daily-history-quality-v1"
_MAX_UNEXPLAINED_CLOSE_MOVE = Decimal("0.35")
_MAX_HISTORICAL_CHUNK_INSTRUMENTS = 2


@dataclass(frozen=True, slots=True)
class IngestDailyHistoryCommand:
    """A synchronized cash/index history refresh with an explicit benchmark."""

    account_id: AccountId
    requests: tuple[DailyHistoryRequest, ...]
    benchmark_id: InstrumentId
    completed_through: date
    required_through: date


@dataclass(frozen=True, slots=True)
class IngestDailyHistoryResult:
    """Counts and completeness evidence after the atomic refresh commits."""

    instruments: int
    bars_added: int
    bars_unchanged: int
    incomplete_bars: int
    required_through: date
    quality_revision: str


@dataclass(frozen=True, slots=True)
class IngestHistoricalDailyHistoryCommand:
    """One bounded historical chunk, complete as of a modern retrieval."""

    account_id: AccountId
    requests: tuple[DailyHistoryRequest, ...]
    benchmark_id: InstrumentId
    completed_through: date


@dataclass(frozen=True, slots=True)
class GetDailyBarSeriesQuery:
    """Point-in-time parameters for one compatible daily series."""

    account_id: AccountId
    instrument_id: InstrumentId
    from_date: date
    to_date: date
    known_at: datetime
    require_complete: bool = True


class IngestDailyHistory:
    """Fetch outside the transaction, validate as a set, then append atomically."""

    __slots__ = ("_source", "_unit_of_work_factory")

    def __init__(
        self,
        source: DailyHistorySource,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        """Bind one read-only source and transaction factory."""
        self._source = source
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, command: IngestDailyHistoryCommand) -> IngestDailyHistoryResult:
        """Reject stale or calendar-incompatible data before any write occurs."""
        self._validate_command(command)
        batches = await fetch_history_batches(self._source, command.requests)
        series = tuple(
            build_daily_series(batch, completed_through=command.completed_through)
            for batch in batches
        )
        for item in series:
            _reject_unexplained_discontinuities(item)
        _validate_synchronized_sessions(
            series,
            benchmark_id=command.benchmark_id,
            required_through=command.required_through,
        )

        added, unchanged = await append_daily_series(
            account_id=command.account_id,
            series=series,
            unit_of_work_factory=self._unit_of_work_factory,
        )

        return IngestDailyHistoryResult(
            instruments=len(series),
            bars_added=added,
            bars_unchanged=unchanged,
            incomplete_bars=sum(
                bar.completeness is BarCompleteness.INCOMPLETE
                for item in series
                for bar in item.bars
            ),
            required_through=command.required_through,
            quality_revision=DAILY_HISTORY_QUALITY_REVISION,
        )

    @staticmethod
    def _validate_command(command: IngestDailyHistoryCommand) -> None:
        """Require one unique, aligned request per instrument and a complete cutoff."""
        if not command.requests:
            raise ValidationError("daily history refresh must contain instruments")
        ids = tuple(item.instrument_id for item in command.requests)
        if len(ids) != len(set(ids)):
            raise ValidationError("daily history refresh contains duplicate instruments")
        if ids.count(command.benchmark_id) != 1:
            raise ValidationError("daily history refresh requires exactly one benchmark")
        ranges = {(item.from_date, item.to_date) for item in command.requests}
        if len(ranges) != 1:
            raise ValidationError("daily history requests must share one date range")
        from_date, to_date = next(iter(ranges))
        if not from_date <= command.required_through <= command.completed_through <= to_date:
            raise ValidationError("daily history cutoffs fall outside the requested range")


class IngestHistoricalDailyHistory:
    """Ingest one independently atomic historical instrument/benchmark chunk."""

    __slots__ = ("_source", "_unit_of_work_factory")

    def __init__(
        self,
        source: DailyHistorySource,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        self._source = source
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self, command: IngestHistoricalDailyHistoryCommand
    ) -> IngestDailyHistoryResult:
        """Fetch sequentially, validate the synchronized chunk, then commit it."""
        self._validate_command(command)
        batches = await fetch_history_batches(self._source, command.requests)
        series = tuple(
            build_daily_series(batch, completed_through=command.completed_through)
            for batch in batches
        )
        for item in series:
            _reject_unexplained_discontinuities(item)
        _validate_synchronized_sessions(
            series,
            benchmark_id=command.benchmark_id,
            required_through=None,
        )
        added, unchanged = await append_daily_series(
            account_id=command.account_id,
            series=series,
            unit_of_work_factory=self._unit_of_work_factory,
        )
        return IngestDailyHistoryResult(
            instruments=len(series),
            bars_added=added,
            bars_unchanged=unchanged,
            incomplete_bars=sum(
                bar.completeness is BarCompleteness.INCOMPLETE
                for item in series
                for bar in item.bars
            ),
            required_through=command.completed_through,
            quality_revision=DAILY_HISTORY_QUALITY_REVISION,
        )

    @staticmethod
    def _validate_command(command: IngestHistoricalDailyHistoryCommand) -> None:
        if not command.requests or len(command.requests) > _MAX_HISTORICAL_CHUNK_INSTRUMENTS:
            raise ValidationError(
                "historical chunk requires a benchmark and at most one owner instrument"
            )
        ids = tuple(item.instrument_id for item in command.requests)
        if len(ids) != len(set(ids)):
            raise ValidationError("historical chunk contains duplicate instruments")
        if ids.count(command.benchmark_id) != 1:
            raise ValidationError("historical chunk requires exactly one benchmark")
        ranges = {(item.from_date, item.to_date) for item in command.requests}
        if len(ranges) != 1:
            raise ValidationError("historical chunk requests must share one date range")
        _from_date, to_date = next(iter(ranges))
        if command.completed_through < to_date:
            raise ValidationError("historical chunk extends beyond its completed cutoff")


class GetDailyBarSeries:
    """Return the latest knowable compatible revisions for one instrument."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, query: GetDailyBarSeriesQuery) -> DailyBarSeries:
        """Resolve bars without allowing later corrections to leak backward."""
        async with self._unit_of_work_factory(query.account_id) as unit_of_work:
            return await unit_of_work.daily_bars.list_series(
                query.instrument_id,
                from_date=query.from_date,
                to_date=query.to_date,
                known_at=query.known_at,
                require_complete=query.require_complete,
            )


async def fetch_history_batches(
    source: DailyHistorySource,
    requests: tuple[DailyHistoryRequest, ...],
) -> tuple[DailyHistoryBatch, ...]:
    """Fetch sequentially and reject provider responses for a different request."""
    fetched: list[DailyHistoryBatch] = []
    provider: str | None = None
    for request in requests:
        batch = await source.fetch(request)
        if batch.request != request:
            raise DataQualityError(
                "daily history source returned a different instrument request",
                requested_instrument_id=str(request.instrument_id),
                returned_instrument_id=str(batch.request.instrument_id),
            )
        if provider is not None and batch.provider != provider:
            raise DataQualityError(
                "daily history refresh mixes providers",
                expected_provider=provider,
                returned_provider=batch.provider,
            )
        provider = batch.provider
        fetched.append(batch)
    return tuple(fetched)


async def append_daily_series(
    *,
    account_id: AccountId,
    series: tuple[DailyBarSeries, ...],
    unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
) -> tuple[int, int]:
    """Append a validated set atomically and return added/unchanged totals."""
    added = 0
    unchanged = 0
    async with unit_of_work_factory(account_id) as unit_of_work:
        for item in series:
            write = await unit_of_work.daily_bars.add_series(item)
            added += write.added
            unchanged += write.unchanged
        await unit_of_work.commit()
    return added, unchanged


def build_daily_series(batch: DailyHistoryBatch, *, completed_through: date) -> DailyBarSeries:
    """Attach persistence provenance and explicit completeness to one provider batch."""
    bars = tuple(
        DailyBarRevision(
            instrument_id=batch.request.instrument_id,
            instrument_kind=batch.request.instrument_kind,
            source=batch.provider,
            source_instrument_id=batch.request.source_instrument_id,
            candle=candle,
            retrieved_at=batch.retrieved_at,
            adjustment_status=batch.adjustment_status,
            completeness=(
                BarCompleteness.COMPLETE
                if candle.trading_date <= completed_through
                else BarCompleteness.INCOMPLETE
            ),
            source_revision=_bar_revision(
                batch.provider,
                candle,
                adjustment_status=batch.adjustment_status.value,
                completeness=(
                    BarCompleteness.COMPLETE
                    if candle.trading_date <= completed_through
                    else BarCompleteness.INCOMPLETE
                ).value,
                quality_revision=DAILY_HISTORY_QUALITY_REVISION,
            ),
            batch_sha256=batch.content_sha256,
            quality_revision=DAILY_HISTORY_QUALITY_REVISION,
        )
        for candle in batch.candles
    )
    return DailyBarSeries(bars=bars)


def _bar_revision(
    provider: str,
    candle: DailyCandle,
    *,
    adjustment_status: str,
    completeness: str,
    quality_revision: str,
) -> str:
    """Hash normalized bar content so range changes do not duplicate unchanged bars."""
    parts = (
        provider,
        candle.trading_date.isoformat(),
        format(candle.open, "f"),
        format(candle.high, "f"),
        format(candle.low, "f"),
        format(candle.close, "f"),
        str(candle.volume),
        "" if candle.open_interest is None else str(candle.open_interest),
        adjustment_status,
        completeness,
        quality_revision,
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _reject_unexplained_discontinuities(series: DailyBarSeries) -> None:
    """Block large close moves until corporate-action provenance explains them."""
    for previous, current in zip(series.bars, series.bars[1:], strict=False):
        change = abs((current.candle.close / previous.candle.close) - Decimal(1))
        if change > _MAX_UNEXPLAINED_CLOSE_MOVE:
            raise DataQualityError(
                "daily history contains a possible corporate-action discontinuity",
                instrument_id=str(current.instrument_id),
                previous_date=previous.candle.trading_date.isoformat(),
                trading_date=current.candle.trading_date.isoformat(),
                quality_revision=DAILY_HISTORY_QUALITY_REVISION,
            )


def _validate_synchronized_sessions(
    series: tuple[DailyBarSeries, ...],
    *,
    benchmark_id: InstrumentId,
    required_through: date | None,
) -> None:
    """Use benchmark sessions as the explicit calendar and reject silent gaps."""
    benchmark = next(item for item in series if item.bars[0].instrument_id == benchmark_id)
    benchmark_dates = {
        item.candle.trading_date
        for item in benchmark.bars
        if (required_through is None or item.candle.trading_date <= required_through)
        and item.completeness is BarCompleteness.COMPLETE
    }
    if required_through is not None and required_through not in benchmark_dates:
        raise StaleDataError(
            "benchmark daily history is stale or incomplete",
            required_through=required_through.isoformat(),
        )
    for item in series:
        dates = {
            bar.candle.trading_date
            for bar in item.bars
            if (required_through is None or bar.candle.trading_date <= required_through)
            and bar.completeness is BarCompleteness.COMPLETE
        }
        if required_through is not None and required_through not in dates:
            raise StaleDataError(
                "instrument daily history is stale or incomplete",
                instrument_id=str(item.bars[0].instrument_id),
                required_through=required_through.isoformat(),
            )
        missing = benchmark_dates - dates
        extra = dates - benchmark_dates
        if missing or extra:
            raise DataQualityError(
                "instrument daily history does not match benchmark sessions",
                instrument_id=str(item.bars[0].instrument_id),
                missing_sessions=len(missing),
                extra_sessions=len(extra),
            )
