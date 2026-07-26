"""Reconstruction factories: persistence record to domain object, and back.

This is the layer Amendment 2 added, and the reason it exists is
:class:`~dhruva.shared.time.TradingDay`. It cannot be constructed without a
calendar (ADR-046). A mapper holding a calendar would stop being a pure function;
a repository constructing one would be reaching for an infrastructure dependency
it should be given. A factory is the thing that legitimately owns domain services
and turns primitives into aggregates.

Deconstruction needs no services and is a pure function, so it lives here only
for symmetry -- keeping both directions in one module means a field added to the
aggregate is obviously missing from one side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.infrastructure.persistence.records import DailySnapshotRecord
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Currency, Money, Price
from dhruva.shared.time import TradingDay

if TYPE_CHECKING:
    from uuid import UUID

    from dhruva.shared.time import TradingCalendar

__all__ = ["DailySnapshotFactory"]


class DailySnapshotFactory:
    """Reconstructs :class:`DailySnapshot` aggregates from persistence records.

    Holds the domain services reconstruction needs. Injected at the composition
    root and handed to the repository, so the repository orchestrates
    reconstruction without embedding it.
    """

    __slots__ = ("_calendar",)

    def __init__(self, calendar: TradingCalendar) -> None:
        """Bind the calendar used to verify trading days on read."""
        self._calendar = calendar

    def reconstruct(self, record: DailySnapshotRecord) -> DailySnapshot:
        """Build the aggregate, verifying its trading day against the calendar.

        Verification on read is deliberate. A stored date that is not a session --
        because a holiday was added retrospectively, or because a bad import
        wrote one -- must surface here rather than propagate into a backtest.
        This is what keeps ADR-046 uncompromised on the read path.
        """
        return DailySnapshot(
            instrument_id=InstrumentId(record.instrument_id),
            trading_day=TradingDay.of(record.trading_day, self._calendar),
            close=Price(record.close_scaled_units, Currency(record.currency)),
            turnover=Money(record.turnover_minor_units, Currency(record.currency)),
            account_id=AccountId(record.account_id),
            version=record.version,
        )

    @staticmethod
    def deconstruct(aggregate: DailySnapshot, row_id: UUID) -> DailySnapshotRecord:
        """Flatten the aggregate into a persistence record.

        ``row_id`` is supplied by the caller because a surrogate primary key is a
        persistence concern: the domain identifies a snapshot by instrument and
        trading day, and knows nothing about the row it lives in.
        """
        return DailySnapshotRecord(
            id=row_id,
            account_id=aggregate.account_id.value,
            instrument_id=aggregate.instrument_id.value,
            trading_day=aggregate.trading_day.on,
            close_scaled_units=aggregate.close.scaled_units,
            turnover_minor_units=aggregate.turnover.minor_units,
            currency=str(aggregate.turnover.currency),
            version=aggregate.version,
        )
