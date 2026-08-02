"""Effective-dated canonical instruments and shared-watchlist membership.

The two histories are separate on purpose. A symbol or company name can change
while the instrument remains on the watchlist, and a user can remove an
instrument without changing what that instrument is. Both carry ``recorded_at``
so a backtest can ask what DHRUVA knew at an earlier instant (ADR-007).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import InvariantViolation, invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "CashInstrumentMapping",
    "InstrumentIdentityRevision",
    "InstrumentKind",
    "WatchlistInstrument",
    "WatchlistMembershipRevision",
]

_EQUITY_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&-]{0,31}\Z")
_INDEX_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9 &-]{0,31}\Z")
_INDIAN_ISIN = re.compile(r"IN[A-Z0-9]{10}\Z")
_MAX_NAME = 200
_MAX_SECTOR = 100
_MAX_SOURCE = 64
_MAX_SOURCE_REVISION = 128


class InstrumentKind(StrEnum):
    """Reference identity kinds needed by the personal MVP."""

    EQUITY = "equity"
    INDEX = "index"


def _require_text(value: str, *, field: str, maximum: int) -> None:
    """Require canonical, bounded text rather than silently normalising it."""
    invariant(bool(value), f"{field} must not be empty", field=field)
    invariant(
        value == value.strip(),
        f"{field} must not have surrounding whitespace",
        field=field,
    )
    invariant(len(value) <= maximum, f"{field} is too long", field=field, maximum=maximum)


def _require_utc(value: datetime, *, field: str) -> None:
    """Require a timezone-aware UTC instant (ADR-006)."""
    invariant(
        value.tzinfo is not None and value.utcoffset() is not None,
        f"{field} must be timezone-aware",
        field=field,
    )
    invariant(
        value.utcoffset() == timedelta(0),
        f"{field} must be UTC",
        field=field,
        value=value.isoformat(),
    )


def _require_window(start: date, end: date | None, *, field: str) -> None:
    """Require an inclusive effective window that never runs backwards."""
    invariant(
        end is None or end >= start,
        f"{field} end cannot precede its start",
        field=field,
        start=start.isoformat(),
        end=end.isoformat() if end is not None else None,
    )


def _require_names(values: tuple[str, ...], *, field: str) -> None:
    """Require a canonical, case-insensitively unique name collection."""
    for value in values:
        _require_text(value, field=field, maximum=_MAX_NAME)
    folded = tuple(value.casefold() for value in values)
    invariant(len(folded) == len(set(folded)), f"{field} must not contain duplicates")
    invariant(
        folded == tuple(sorted(folded)),
        f"{field} must be deterministically sorted",
        field=field,
    )


@dataclass(frozen=True, slots=True)
class CashInstrumentMapping:
    """The effective NSE cash instrument used for data-provider lookups.

    Broker tokens are optional because the owner-approved watchlist exists before
    the first instrument-master refresh. They are attributes, never keys
    (ADR-009), and must arrive as a complete pair.
    """

    exchange: str
    trading_symbol: str
    provider: str | None = None
    instrument_token: int | None = None
    exchange_token: int | None = None

    def __post_init__(self) -> None:
        """Reject incomplete or non-canonical provider mappings."""
        _require_text(self.exchange, field="exchange", maximum=16)
        invariant(self.exchange == self.exchange.upper(), "exchange must be uppercase")
        invariant(bool(_INDEX_SYMBOL.fullmatch(self.trading_symbol)), "invalid NSE trading symbol")
        pair = (self.instrument_token is None, self.exchange_token is None)
        invariant(pair[0] == pair[1], "instrument and exchange tokens form a pair")
        if self.instrument_token is None:
            invariant(self.provider is None, "a provider without tokens is not a mapping")
            return
        if self.provider is None:
            raise InvariantViolation("provider is required when tokens are present")
        _require_text(self.provider, field="provider", maximum=32)
        invariant(self.instrument_token > 0, "instrument token must be positive")
        invariant(
            self.exchange_token is not None and self.exchange_token > 0,
            "exchange token must be positive",
        )


@dataclass(frozen=True, slots=True)
class InstrumentIdentityRevision:
    """One point-in-time revision of an instrument's effective identity."""

    instrument_id: InstrumentId
    kind: InstrumentKind
    canonical_symbol: str
    company_name: str
    aliases: tuple[str, ...]
    former_names: tuple[str, ...]
    isin: str | None
    sector: str
    concentration_groups: tuple[str, ...]
    cash_mapping: CashInstrumentMapping
    futures_research_requested: bool
    valid_from: date
    valid_to: date | None
    recorded_at: datetime
    source: str
    source_revision: str

    def __post_init__(self) -> None:
        """Reject an identity that cannot be matched or replayed safely."""
        symbol_pattern = _EQUITY_SYMBOL if self.kind is InstrumentKind.EQUITY else _INDEX_SYMBOL
        invariant(
            bool(symbol_pattern.fullmatch(self.canonical_symbol)), "invalid canonical NSE symbol"
        )
        _require_text(self.company_name, field="company_name", maximum=_MAX_NAME)
        _require_text(self.sector, field="sector", maximum=_MAX_SECTOR)
        _require_text(self.source, field="source", maximum=_MAX_SOURCE)
        _require_text(
            self.source_revision,
            field="source_revision",
            maximum=_MAX_SOURCE_REVISION,
        )
        _require_names(self.aliases, field="aliases")
        _require_names(self.former_names, field="former_names")
        _require_names(self.concentration_groups, field="concentration_groups")
        all_names = tuple(name.casefold() for name in (*self.aliases, *self.former_names))
        invariant(len(all_names) == len(set(all_names)), "aliases and former names overlap")
        if self.isin is not None:
            invariant(bool(_INDIAN_ISIN.fullmatch(self.isin)), "invalid Indian ISIN")
        invariant(self.cash_mapping.exchange == "NSE", "personal MVP cash mappings are NSE-only")
        invariant(
            self.cash_mapping.trading_symbol == self.canonical_symbol,
            "cash mapping and canonical symbol must agree",
        )
        _require_window(self.valid_from, self.valid_to, field="identity validity")
        _require_utc(self.recorded_at, field="recorded_at")

    def effective_on(self, day: date) -> bool:
        """Return whether this identity applies on ``day`` (inclusive bounds)."""
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True, slots=True)
class WatchlistMembershipRevision:
    """One point-in-time revision of tenant-shared watchlist membership."""

    account_id: AccountId
    instrument_id: InstrumentId
    active_from: date
    active_to: date | None
    recorded_at: datetime
    source: str
    source_revision: str

    def __post_init__(self) -> None:
        """Reject a membership revision that cannot be replayed."""
        _require_window(self.active_from, self.active_to, field="membership activity")
        _require_utc(self.recorded_at, field="recorded_at")
        _require_text(self.source, field="source", maximum=_MAX_SOURCE)
        _require_text(
            self.source_revision,
            field="source_revision",
            maximum=_MAX_SOURCE_REVISION,
        )

    def active_on(self, day: date) -> bool:
        """Return whether the instrument belongs to the watchlist on ``day``."""
        return self.active_from <= day and (self.active_to is None or day <= self.active_to)


@dataclass(frozen=True, slots=True)
class WatchlistInstrument:
    """An effective instrument identity joined to active watchlist membership."""

    identity: InstrumentIdentityRevision
    membership: WatchlistMembershipRevision

    def __post_init__(self) -> None:
        """Require both histories to describe the same stable instrument."""
        invariant(
            self.identity.instrument_id == self.membership.instrument_id,
            "identity and watchlist membership must name the same instrument",
        )
