"""Primitive daily-bar records passed to the ORM-bypass append path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

__all__ = ["DailyBarRecord"]


@dataclass(frozen=True, slots=True)
class DailyBarRecord:
    """Framework-free primitive representation of one daily bar revision."""

    id: UUID
    instrument_id: UUID
    instrument_kind: str
    trading_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    open_interest: int | None
    source: str
    source_instrument_id: int
    retrieved_at: datetime
    adjustment_status: str
    completeness: str
    source_revision: str
    batch_sha256: str
    quality_revision: str
