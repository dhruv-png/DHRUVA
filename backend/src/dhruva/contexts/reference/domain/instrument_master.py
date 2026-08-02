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
    "ArchivedInstrumentDiscovery",
    "ArchivedInstrumentMaster",
    "FuturesAvailability",
    "FuturesAvailabilityStatus",
    "FuturesContract",
    "FuturesContractObservation",
    "FuturesContractStatus",
    "InstrumentArchiveWrite",
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
_MAX_RESOLVER_REVISION = 64


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


def _validate_resolution_provenance(
    *,
    provider: str,
    market_date: date,
    resolutions: tuple[InstrumentResolution, ...],
) -> None:
    """Require every resolved mapping to belong to the enclosing snapshot."""
    for resolution in resolutions:
        if resolution.cash is not None:
            invariant(
                resolution.cash.provider == provider,
                "cash mapping provider differs from snapshot provider",
            )
            invariant(
                resolution.cash.instrument_id == resolution.instrument_id,
                "cash mapping instrument differs from resolution instrument",
            )
        for contract in resolution.futures.contracts:
            invariant(
                contract.provider == provider,
                "futures contract provider differs from snapshot provider",
            )
            invariant(
                contract.underlying_id == resolution.instrument_id,
                "futures contract underlying differs from resolution instrument",
            )
            invariant(contract.expiry >= market_date, "active futures contract is expired")
        for observation in resolution.futures_observations:
            contract = observation.contract
            invariant(
                contract.provider == provider,
                "futures observation provider differs from snapshot provider",
            )
            invariant(
                contract.underlying_id == resolution.instrument_id,
                "futures observation underlying differs from resolution instrument",
            )
            expected_status = (
                FuturesContractStatus.ACTIVE
                if contract.expiry >= market_date
                else FuturesContractStatus.EXPIRED
            )
            invariant(
                observation.status is expected_status,
                "futures observation status differs from its expiry",
            )


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
class ArchivedInstrumentMaster:
    """Replayable persisted master without duplicating every parsed provider row."""

    provider: str
    market_date: date
    fetched_at: datetime
    content_sha256: str
    raw_csv: bytes
    row_count: int

    def __post_init__(self) -> None:
        """Apply the same provenance checks used before the source was archived."""
        invariant(bool(_PROVIDER_TEXT.fullmatch(self.provider)), "invalid provider name")
        _utc(self.fetched_at, field="fetched_at")
        invariant(bool(_HEX_SHA256.fullmatch(self.content_sha256)), "invalid SHA-256 digest")
        invariant(bool(self.raw_csv), "instrument master must not be empty")
        invariant(
            hashlib.sha256(self.raw_csv).hexdigest() == self.content_sha256,
            "instrument master digest does not match its bytes",
        )
        invariant(self.row_count > 0, "instrument master row count must be positive")


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


class FuturesContractStatus(StrEnum):
    """Lifecycle state of an actual contract on the snapshot market date."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class FuturesContractObservation:
    """One unambiguous actual contract row and its dated lifecycle state."""

    contract: FuturesContract
    status: FuturesContractStatus
    selected_for_availability: bool

    def __post_init__(self) -> None:
        """Prevent an expired contract from evidencing current availability."""
        if self.selected_for_availability:
            invariant(
                self.status is FuturesContractStatus.ACTIVE,
                "only active contracts can evidence current availability",
            )


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
    futures_observations: tuple[FuturesContractObservation, ...]

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
        observed_expiries = tuple(
            observation.contract.expiry for observation in self.futures_observations
        )
        invariant(
            observed_expiries == tuple(sorted(observed_expiries)),
            "futures observations must be sorted by expiry",
        )
        invariant(
            len(observed_expiries) == len(set(observed_expiries)),
            "one unambiguous observation is allowed per expiry",
        )
        selected = tuple(
            observation.contract
            for observation in self.futures_observations
            if observation.selected_for_availability
        )
        invariant(
            selected == self.futures.contracts,
            "selected observations must equal current availability contracts",
        )


@dataclass(frozen=True, slots=True)
class InstrumentDiscovery:
    """Complete fixture-backed or real result of one daily master refresh."""

    snapshot: InstrumentMasterSnapshot
    resolutions: tuple[InstrumentResolution, ...]
    resolver_revision: str = "instrument-discovery-v1"

    def __post_init__(self) -> None:
        """Require exactly one deterministic result per underlying."""
        symbols = tuple(item.canonical_symbol for item in self.resolutions)
        invariant(symbols == tuple(sorted(symbols)), "resolutions must be sorted by symbol")
        invariant(len(symbols) == len(set(symbols)), "resolution symbols must be unique")
        _validate_resolution_provenance(
            provider=self.snapshot.provider,
            market_date=self.snapshot.market_date,
            resolutions=self.resolutions,
        )
        _bounded_text(
            self.resolver_revision,
            field="resolver_revision",
            maximum=_MAX_RESOLVER_REVISION,
        )


@dataclass(frozen=True, slots=True)
class ArchivedInstrumentDiscovery:
    """One replayable snapshot and its versioned owner-universe resolution."""

    snapshot: ArchivedInstrumentMaster
    resolutions: tuple[InstrumentResolution, ...]
    resolver_revision: str

    def __post_init__(self) -> None:
        """Keep reconstructed archive results deterministic and self-identifying."""
        symbols = tuple(item.canonical_symbol for item in self.resolutions)
        invariant(symbols == tuple(sorted(symbols)), "resolutions must be sorted by symbol")
        invariant(len(symbols) == len(set(symbols)), "resolution symbols must be unique")
        _validate_resolution_provenance(
            provider=self.snapshot.provider,
            market_date=self.snapshot.market_date,
            resolutions=self.resolutions,
        )
        _bounded_text(
            self.resolver_revision,
            field="resolver_revision",
            maximum=_MAX_RESOLVER_REVISION,
        )


@dataclass(frozen=True, slots=True)
class InstrumentArchiveWrite:
    """Counts from one atomic append, distinguishing facts from retries."""

    snapshot_added: int
    resolutions_added: int
    cash_mappings_added: int
    contracts_added: int
    contract_revisions_added: int

    def __post_init__(self) -> None:
        """Repository counts cannot be negative or claim multiple daily snapshots."""
        invariant(self.snapshot_added in {0, 1}, "snapshot count must be zero or one")
        invariant(self.resolutions_added >= 0, "resolution count cannot be negative")
        invariant(self.cash_mappings_added >= 0, "cash mapping count cannot be negative")
        invariant(self.contracts_added >= 0, "contract count cannot be negative")
        invariant(self.contract_revisions_added >= 0, "contract revision count cannot be negative")
