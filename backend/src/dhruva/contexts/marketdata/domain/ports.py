"""Inward-facing daily market-data source and persistence contracts."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.contexts.marketdata.domain.daily_bars import (
        DailyBarArchiveWrite,
        DailyBarSeries,
        DailyHistoryBatch,
        DailyHistoryRequest,
    )
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "DailyBarStore",
    "DailyHistorySource",
    "HistoricalMarketDataProvider",
    "MarketDataUnitOfWork",
]


@runtime_checkable
class DailyHistorySource(Protocol):
    """Read historical daily candles through a provider-neutral request."""

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Return one bounded, replayable provider response."""
        ...


@runtime_checkable
class HistoricalMarketDataProvider(DailyHistorySource, Protocol):
    """Deep-history source using the same narrow daily request contract.

    Coverage, delistings, adjustment semantics, and data rights are separate
    readiness evidence; they do not belong on a giant generic adapter.
    """


@runtime_checkable
class DailyBarStore(Protocol):
    """Append and query point-in-knowledge-time daily bars; never commit."""

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Stage new bar revisions and report identical retries."""
        ...

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,
    ) -> DailyBarSeries:
        """Resolve the latest knowable revision for every requested date."""
        ...


@runtime_checkable
class MarketDataUnitOfWork(Protocol):
    """One transaction containing daily market-data revisions."""

    @property
    def daily_bars(self) -> DailyBarStore:
        """Return the daily-bar store bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless commit succeeded."""
        ...

    async def commit(self) -> None:
        """Commit every staged market-data revision."""
        ...

    async def rollback(self) -> None:
        """Discard every staged market-data revision."""
        ...
