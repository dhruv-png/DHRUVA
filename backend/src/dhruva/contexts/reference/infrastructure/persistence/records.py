"""Primitive records between reference-domain factories and SQLAlchemy models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

__all__ = ["IdentityRevisionRecord", "MembershipRevisionRecord"]


@dataclass(frozen=True, slots=True)
class IdentityRevisionRecord:
    """Primitive representation of an instrument identity revision."""

    id: UUID
    instrument_id: UUID
    kind: str
    canonical_symbol: str
    company_name: str
    aliases: tuple[str, ...]
    former_names: tuple[str, ...]
    isin: str | None
    sector: str
    concentration_groups: tuple[str, ...]
    cash_exchange: str
    cash_trading_symbol: str
    provider: str | None
    instrument_token: int | None
    exchange_token: int | None
    futures_research_requested: bool
    valid_from: date
    valid_to: date | None
    recorded_at: datetime
    source: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class MembershipRevisionRecord:
    """Primitive representation of a watchlist membership revision."""

    id: UUID
    account_id: UUID
    instrument_id: UUID
    active_from: date
    active_to: date | None
    recorded_at: datetime
    source: str
    source_revision: str
