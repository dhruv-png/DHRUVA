"""Resolve approved stable identities against a daily instrument master."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.reference.domain.instrument_master import (
    FuturesAvailability,
    FuturesAvailabilityStatus,
    FuturesContract,
    InstrumentDiscovery,
    InstrumentResolution,
    ResolvedCashInstrument,
)
from dhruva.contexts.reference.domain.watchlist import InstrumentKind
from dhruva.shared.errors import StaleDataError
from dhruva.shared.identity import InstrumentId

if TYPE_CHECKING:
    from datetime import date

    from dhruva.contexts.reference.application.watchlist import UniverseDefinition
    from dhruva.contexts.reference.domain.instrument_master import (
        InstrumentMasterEntry,
        InstrumentMasterSnapshot,
    )
    from dhruva.contexts.reference.domain.ports import InstrumentMasterSource

__all__ = ["DiscoverOwnerInstruments", "DiscoverOwnerInstrumentsCommand"]


@dataclass(frozen=True, slots=True)
class DiscoverOwnerInstrumentsCommand:
    """Owner configuration and the trading date to resolve."""

    definitions: tuple[UniverseDefinition, ...]
    market_date: date


class DiscoverOwnerInstruments:
    """Map current provider facts without assuming futures eligibility."""

    __slots__ = ("_source",)

    def __init__(self, source: InstrumentMasterSource) -> None:
        """Bind one read-only provider adapter."""
        self._source = source

    async def execute(self, command: DiscoverOwnerInstrumentsCommand) -> InstrumentDiscovery:
        """Return explicit cash and futures availability for every definition."""
        snapshot = await self._source.fetch(market_date=command.market_date)
        if snapshot.market_date != command.market_date:
            raise StaleDataError(
                "instrument master is assigned to a different trading date",
                requested=command.market_date.isoformat(),
                received=snapshot.market_date.isoformat(),
            )

        resolutions = tuple(
            sorted(
                (
                    _resolve_definition(definition, snapshot, command.market_date)
                    for definition in command.definitions
                ),
                key=lambda item: item.canonical_symbol,
            )
        )
        return InstrumentDiscovery(snapshot=snapshot, resolutions=resolutions)


def _resolve_definition(
    definition: UniverseDefinition,
    snapshot: InstrumentMasterSnapshot,
    market_date: date,
) -> InstrumentResolution:
    """Resolve one stable identity without allowing a name-only cash match."""
    instrument_id = InstrumentId.deterministic("reference", definition.identity_key)
    cash_candidates = tuple(
        entry
        for entry in snapshot.entries
        if entry.exchange == "NSE"
        and entry.trading_symbol == definition.canonical_symbol
        and _cash_kind_matches(definition.kind, entry)
    )
    cash: ResolvedCashInstrument | None = None
    cash_reason: str | None = None
    if len(cash_candidates) == 1:
        row = cash_candidates[0]
        cash = ResolvedCashInstrument(
            instrument_id=instrument_id,
            provider=snapshot.provider,
            exchange=row.exchange,
            trading_symbol=row.trading_symbol,
            instrument_token=row.instrument_token,
            exchange_token=row.exchange_token,
        )
    elif not cash_candidates:
        cash_reason = "No exact current NSE cash mapping was present in the instrument master."
    else:
        cash_reason = "Multiple exact NSE cash mappings were present; resolution is ambiguous."

    futures = _resolve_futures(
        definition,
        instrument_id=instrument_id,
        snapshot=snapshot,
        market_date=market_date,
    )
    return InstrumentResolution(
        instrument_id=instrument_id,
        canonical_symbol=definition.canonical_symbol,
        cash=cash,
        cash_unavailable_reason=cash_reason,
        futures=futures,
    )


def _cash_kind_matches(kind: InstrumentKind, entry: InstrumentMasterEntry) -> bool:
    """Accept only the documented cash row shape for the stable identity kind."""
    if kind is InstrumentKind.EQUITY:
        return entry.segment == "NSE" and entry.instrument_type == "EQ"
    return entry.segment == "INDICES" and entry.instrument_type not in {"FUT", "CE", "PE"}


def _resolve_futures(
    definition: UniverseDefinition,
    *,
    instrument_id: InstrumentId,
    snapshot: InstrumentMasterSnapshot,
    market_date: date,
) -> FuturesAvailability:
    """Find all active, well-formed NFO futures for one approved underlying."""
    if not definition.futures_research_requested:
        return FuturesAvailability(
            status=FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE,
            reason="Futures research is not enabled for this underlying.",
            contracts=(),
        )

    provider_names = {definition.canonical_symbol}
    if definition.kind is InstrumentKind.INDEX and definition.canonical_symbol == "NIFTY 50":
        provider_names.add("NIFTY")
    candidates = tuple(
        entry
        for entry in snapshot.entries
        if entry.exchange == "NFO"
        and entry.segment == "NFO-FUT"
        and entry.instrument_type == "FUT"
        and entry.name in provider_names
        and entry.expiry is not None
        and entry.expiry >= market_date
        and entry.lot_size > 0
        and entry.tick_size > 0
    )
    by_expiry: dict[date, InstrumentMasterEntry] = {}
    ambiguous_expiries: set[date] = set()
    for entry in candidates:
        expiry = entry.expiry
        if expiry is None:  # pragma: no cover - excluded by the candidate predicate
            continue
        if expiry in by_expiry:
            ambiguous_expiries.add(expiry)
        else:
            by_expiry[expiry] = entry
    if ambiguous_expiries:
        return FuturesAvailability(
            status=FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE,
            reason="Current NFO futures mapping is ambiguous for one or more expiries.",
            contracts=(),
        )

    contracts = tuple(
        _contract(instrument_id, snapshot.provider, expiry, entry)
        for expiry, entry in sorted(by_expiry.items())
    )
    if not contracts:
        return FuturesAvailability(
            status=FuturesAvailabilityStatus.CURRENTLY_UNAVAILABLE,
            reason="No active NFO-FUT contract with positive lot and tick size was found.",
            contracts=(),
        )
    return FuturesAvailability(
        status=FuturesAvailabilityStatus.AVAILABLE,
        reason="One or more active NFO-FUT contracts were resolved unambiguously.",
        contracts=contracts,
    )


def _contract(
    underlying_id: InstrumentId,
    provider: str,
    expiry: date,
    entry: InstrumentMasterEntry,
) -> FuturesContract:
    """Create a provider-independent identity for one actual contract month."""
    contract_id = InstrumentId.deterministic(
        "futures-contract",
        str(underlying_id.value),
        entry.exchange,
        expiry.isoformat(),
    )
    return FuturesContract(
        contract_id=contract_id,
        underlying_id=underlying_id,
        provider=provider,
        instrument_token=entry.instrument_token,
        exchange_token=entry.exchange_token,
        trading_symbol=entry.trading_symbol,
        expiry=expiry,
        lot_size=entry.lot_size,
        tick_size=entry.tick_size,
        instrument_type=entry.instrument_type,
        segment=entry.segment,
        exchange=entry.exchange,
    )
