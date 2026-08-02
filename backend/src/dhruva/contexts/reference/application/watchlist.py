"""Configure and query the owner-approved shared reference universe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
    WatchlistMembershipRevision,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date, datetime

    from dhruva.contexts.reference.domain.ports import ReferenceUnitOfWork
    from dhruva.contexts.reference.domain.watchlist import WatchlistInstrument
    from dhruva.shared.identity import AccountId

__all__ = [
    "ConfigureReferenceUniverse",
    "ConfigureReferenceUniverseCommand",
    "ConfigureReferenceUniverseResult",
    "GetSharedWatchlist",
    "UniverseDefinition",
]

_MAX_SHARED_WATCHLIST = 50


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    """Input definition for one stable underlying and one identity revision."""

    identity_key: str
    kind: InstrumentKind
    canonical_symbol: str
    company_name: str
    aliases: tuple[str, ...]
    former_names: tuple[str, ...]
    isin: str | None
    sector: str
    concentration_groups: tuple[str, ...]
    effective_from: date
    effective_to: date | None
    included_in_watchlist: bool
    futures_research_requested: bool


@dataclass(frozen=True, slots=True)
class ConfigureReferenceUniverseCommand:
    """A complete, versioned owner configuration to persist idempotently."""

    account_id: AccountId
    definitions: tuple[UniverseDefinition, ...]
    recorded_at: datetime
    source: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class ConfigureReferenceUniverseResult:
    """Counts distinguishing new facts from idempotent retries."""

    identities_added: int
    memberships_added: int
    identities_unchanged: int
    memberships_unchanged: int


class ConfigureReferenceUniverse:
    """Persist one versioned universe configuration in one transaction."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], ReferenceUnitOfWork],
    ) -> None:
        """Bind the use case to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        command: ConfigureReferenceUniverseCommand,
    ) -> ConfigureReferenceUniverseResult:
        """Write every identity and selected membership, then commit atomically."""
        self._validate(command)
        identities_added = 0
        memberships_added = 0
        identities_unchanged = 0
        memberships_unchanged = 0

        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            for definition in command.definitions:
                instrument_id = InstrumentId.deterministic(
                    "reference",
                    definition.identity_key,
                )
                identity = InstrumentIdentityRevision(
                    instrument_id=instrument_id,
                    kind=definition.kind,
                    canonical_symbol=definition.canonical_symbol,
                    company_name=definition.company_name,
                    aliases=definition.aliases,
                    former_names=definition.former_names,
                    isin=definition.isin,
                    sector=definition.sector,
                    concentration_groups=definition.concentration_groups,
                    cash_mapping=CashInstrumentMapping(
                        exchange="NSE",
                        trading_symbol=definition.canonical_symbol,
                    ),
                    futures_research_requested=definition.futures_research_requested,
                    valid_from=definition.effective_from,
                    valid_to=definition.effective_to,
                    recorded_at=command.recorded_at,
                    source=command.source,
                    source_revision=command.source_revision,
                )
                if await unit_of_work.reference.add_identity(identity):
                    identities_added += 1
                else:
                    identities_unchanged += 1

                if not definition.included_in_watchlist:
                    continue
                membership = WatchlistMembershipRevision(
                    account_id=command.account_id,
                    instrument_id=instrument_id,
                    active_from=definition.effective_from,
                    active_to=definition.effective_to,
                    recorded_at=command.recorded_at,
                    source=command.source,
                    source_revision=command.source_revision,
                )
                if await unit_of_work.reference.add_membership(membership):
                    memberships_added += 1
                else:
                    memberships_unchanged += 1
            await unit_of_work.commit()

        return ConfigureReferenceUniverseResult(
            identities_added=identities_added,
            memberships_added=memberships_added,
            identities_unchanged=identities_unchanged,
            memberships_unchanged=memberships_unchanged,
        )

    @staticmethod
    def _validate(command: ConfigureReferenceUniverseCommand) -> None:
        """Reject an ambiguous or oversized owner configuration."""
        watchlist = tuple(item for item in command.definitions if item.included_in_watchlist)
        if len(watchlist) > _MAX_SHARED_WATCHLIST:
            raise ValidationError(
                "shared watchlist exceeds the configured maximum",
                count=len(watchlist),
                maximum=_MAX_SHARED_WATCHLIST,
            )
        if any(item.kind is not InstrumentKind.EQUITY for item in watchlist):
            raise ValidationError("the shared watchlist may contain cash equities only")
        keys = tuple(item.identity_key for item in command.definitions)
        symbols = tuple(item.canonical_symbol for item in command.definitions)
        if len(keys) != len(set(keys)):
            raise ValidationError("universe identity keys must be unique")
        if len(symbols) != len(set(symbols)):
            raise ValidationError("canonical symbols must be unique within a universe revision")


class GetSharedWatchlist:
    """Return the effective tenant-shared watchlist at a point in knowledge time."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], ReferenceUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        *,
        account_id: AccountId,
        effective_on: date,
        known_at: datetime,
    ) -> tuple[WatchlistInstrument, ...]:
        """Read only revisions that were knowable by ``known_at``."""
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            return await unit_of_work.reference.list_watchlist(
                account_id,
                effective_on=effective_on,
                known_at=known_at,
            )
