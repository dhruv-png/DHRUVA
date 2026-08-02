"""Provider-neutral instrument-master facts and resolved research instruments.

The provider dump is evidence, not identity. Tokens and trading symbols may
change, while the stable :class:`~dhruva.shared.identity.InstrumentId` remains
the same (ADR-009). Futures availability is therefore a dated result rather
than a property of the owner's static watchlist.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "FuturesAvailability",
    "FuturesAvailabilityStatus",
    "FuturesContract",
    "InstrumentDiscovery",
    "InstrumentMasterEntry",
    "InstrumentMasterSnapshot",
    "InstrumentResolution",
    "ResolvedCashInstrument",
]

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_TEXT = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
_MAX_SYMBOL = 64
_MAX_NAME = 200
_MAX_REASON = 240


def _bounded_text(value: str, *, field: str, maximum: int) -> None:
    invariant(bool(value), f"{field} must not be empty", field=field)
    invariant(value == value.strip(), f"{field} must not have surrounding whitespace")
    invariant(len(value) <= maximum, f"{field} is too long", maximum=maximum)


def _utc(value: datetime, *, field: str) -> None:
    invariant(
        value.tzinfo is not None and value.utcoffset() is not None,
        f"{field} must be timezone-aware",
    )
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")


@dataclass(frozen=True, slots=True)
class InstrumentMasterEntry:
    """One normalized row from a provider instrument master."""

    instrument_token: int
    exchange_token: int
    trading_symbol: str
    name: str
    expiry: date | None
    strike: Decimal
    tick_size: Decimal
    lot_size: int
    instrument_type: str
    segment: str
    exchange: str

    def __post_init__(self) -> None:
        """Reject ambiguous identifiers and nonsensical numeric fields."""
        invariant(self.instrument_token > 0, "instrument token must be positive")
        invariant(self.exchange_token > 0, "exchange token must be positive")
        _bounded_text(self.trading_symbol, field="trading_symbol", maximum=_MAX_SYMBOL)
        invariant(len(self.name) <= _MAX_NAME, "instrument name is too long")
        invariant(self.name == self.name.strip(), "instrument name has surrounding whitespace")
        _bounded_text(self.instrument_type, field="instrument_type", maximum=16)
        _bounded_text(self.segment, field="segment", maximum=32)
        _bounded_text(self.exchange, field="exchange", maximum=16)
        invariant(self.strike >= 0, "strike cannot be negative")
        invariant(self.tick_size >= 0, "tick size cannot be negative")
        invariant(self.lot_size >= 0, "lot size cannot be negative")


@dataclass(frozen=True, slots=True)
class InstrumentMasterSnapshot:
    """One immutable daily provider dump prepared for archive and resolution."""

    provider: str
    market_date: date
    fetched_at: datetime
    content_sha256: str
    raw_csv: bytes
    entries: tuple[InstrumentMasterEntry, ...]

    def __post_init__(self) -> None:
        """Require replayable content with a trustworthy digest."""
        invariant(bool(_PROVIDER_TEXT.fullmatch(self.provider)), "invalid provider name")
        _utc(self.fetched_at, field="fetched_at")
        invariant(bool(_HEX_SHA256.fullmatch(self.content_sha256)), "invalid SHA-256 digest")
        invariant(bool(self.raw_csv), "instrument master must not be empty")
        invariant(
            hashlib.sha256(self.raw_csv).hexdigest() == self.content_sha256,
            "instrument master digest does not match its bytes",
        )
        invariant(bool(self.entries), "instrument master must contain rows")
        tokens = tuple(item.instrument_token for item in self.entries)
        invariant(len(tokens) == len(set(tokens)), "instrument tokens must be unique")


@dataclass(frozen=True, slots=True)
class ResolvedCashInstrument:
    """Current provider mapping for one stable cash or index identity."""

    instrument_id: InstrumentId
    provider: str
    exchange: str
    trading_symbol: str
    instrument_token: int
    exchange_token: int

    def __post_init__(self) -> None:
        """Require a complete current provider mapping."""
        invariant(bool(_PROVIDER_TEXT.fullmatch(self.provider)), "invalid provider name")
        invariant(self.exchange == "NSE", "cash research instruments must use NSE")
        _bounded_text(self.trading_symbol, field="trading_symbol", maximum=_MAX_SYMBOL)
        invariant(self.instrument_token > 0, "instrument token must be positive")
        invariant(self.exchange_token > 0, "exchange token must be positive")


@dataclass(frozen=True, slots=True)
class FuturesContract:
    """One active actual contract, separate from its stable underlying."""

    contract_id: InstrumentId
    underlying_id: InstrumentId
    provider: str
    instrument_token: int
    exchange_token: int
    trading_symbol: str
    expiry: date
    lot_size: int
    tick_size: Decimal
    instrument_type: str
    segment: str
    exchange: str

    def __post_init__(self) -> None:
        """Require exactly the NSE futures contract shape approved by the owner."""
        invariant(
            self.contract_id != self.underlying_id, "contract and underlying identities differ"
        )
        invariant(bool(_PROVIDER_TEXT.fullmatch(self.provider)), "invalid provider name")
        invariant(self.instrument_token > 0, "instrument token must be positive")
        invariant(self.exchange_token > 0, "exchange token must be positive")
        _bounded_text(self.trading_symbol, field="trading_symbol", maximum=_MAX_SYMBOL)
        invariant(self.lot_size > 0, "futures lot size must be positive")
        invariant(self.tick_size > 0, "futures tick size must be positive")
        invariant(self.instrument_type == "FUT", "instrument type must be FUT")
        invariant(self.segment == "NFO-FUT", "segment must be NFO-FUT")
        invariant(self.exchange == "NFO", "futures exchange must be NFO")


class FuturesAvailabilityStatus(StrEnum):
    """The dated result of current-contract discovery."""

    AVAILABLE = "AVAILABLE"
    CURRENTLY_UNAVAILABLE = "CURRENTLY_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FuturesAvailability:
    """Current futures state for an owner-approved underlying."""

    status: FuturesAvailabilityStatus
    reason: str
    contracts: tuple[FuturesContract, ...]

    def __post_init__(self) -> None:
        """Keep status, reason and contract evidence consistent."""
        _bounded_text(self.reason, field="reason", maximum=_MAX_REASON)
        expiries = tuple(contract.expiry for contract in self.contracts)
        invariant(expiries == tuple(sorted(expiries)), "contracts must be sorted by expiry")
        invariant(len(expiries) == len(set(expiries)), "one underlying has one future per expiry")
        if self.status is FuturesAvailabilityStatus.AVAILABLE:
            invariant(bool(self.contracts), "available futures require at least one contract")
        else:
            invariant(not self.contracts, "unavailable futures cannot carry contracts")


@dataclass(frozen=True, slots=True)
class InstrumentResolution:
    """Cash mapping and dated futures state for one approved underlying."""

    instrument_id: InstrumentId
    canonical_symbol: str
    cash: ResolvedCashInstrument | None
    cash_unavailable_reason: str | None
    futures: FuturesAvailability

    def __post_init__(self) -> None:
        """Expose absence explicitly; never manufacture a mapping."""
        _bounded_text(self.canonical_symbol, field="canonical_symbol", maximum=_MAX_SYMBOL)
        invariant(
            (self.cash is None) == (self.cash_unavailable_reason is not None),
            "cash mapping absence must include exactly one reason",
        )
        if self.cash_unavailable_reason is not None:
            _bounded_text(
                self.cash_unavailable_reason,
                field="cash_unavailable_reason",
                maximum=_MAX_REASON,
            )


@dataclass(frozen=True, slots=True)
class InstrumentDiscovery:
    """Complete fixture-backed or real result of one daily master refresh."""

    snapshot: InstrumentMasterSnapshot
    resolutions: tuple[InstrumentResolution, ...]

    def __post_init__(self) -> None:
        """Require exactly one deterministic result per underlying."""
        symbols = tuple(item.canonical_symbol for item in self.resolutions)
        invariant(symbols == tuple(sorted(symbols)), "resolutions must be sorted by symbol")
        invariant(len(symbols) == len(set(symbols)), "resolution symbols must be unique")
