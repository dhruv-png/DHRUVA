"""Persistence records: the shape of a row, in primitives only.

A record sits between the SQLAlchemy model and the domain object. It carries no
domain types, so a mapper can produce one without needing a calendar, a currency
policy, or any other domain service.

Frozen and slotted because millions of these are created on a read path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

__all__ = ["DailySnapshotRecord"]


@dataclass(frozen=True, slots=True)
class DailySnapshotRecord:
    """A ``daily_snapshot`` row, as primitives.

    Note ``trading_day`` is a plain :class:`datetime.date`. Lifting it to a
    ``TradingDay`` requires a calendar and happens in the factory.
    """

    id: UUID
    account_id: UUID
    instrument_id: UUID
    trading_day: date
    close_scaled_units: int
    turnover_minor_units: int
    currency: str
    version: int
