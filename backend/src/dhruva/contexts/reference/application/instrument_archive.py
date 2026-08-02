"""Fetch, resolve and atomically archive one daily instrument master."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.reference.application.instrument_discovery import (
    DiscoverOwnerInstruments,
    DiscoverOwnerInstrumentsCommand,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from dhruva.contexts.reference.application.watchlist import UniverseDefinition
    from dhruva.contexts.reference.domain.instrument_master import (
        ArchivedInstrumentDiscovery,
        InstrumentArchiveWrite,
    )
    from dhruva.contexts.reference.domain.ports import (
        InstrumentArchiveUnitOfWork,
        InstrumentMasterSource,
    )
    from dhruva.shared.identity import AccountId

__all__ = [
    "ArchiveOwnerInstrumentMaster",
    "ArchiveOwnerInstrumentMasterCommand",
    "ArchiveOwnerInstrumentMasterResult",
    "GetArchivedInstrumentDiscovery",
]


@dataclass(frozen=True, slots=True)
class ArchiveOwnerInstrumentMasterCommand:
    """Owner universe and trading date for one provider refresh."""

    account_id: AccountId
    definitions: tuple[UniverseDefinition, ...]
    market_date: date


@dataclass(frozen=True, slots=True)
class ArchiveOwnerInstrumentMasterResult:
    """Provenance and append counts returned after the transaction commits."""

    provider: str
    market_date: date
    content_sha256: str
    resolver_revision: str
    write: InstrumentArchiveWrite


class ArchiveOwnerInstrumentMaster:
    """Keep provider I/O outside the transaction, then archive atomically."""

    __slots__ = ("_discover", "_unit_of_work_factory")

    def __init__(
        self,
        source: InstrumentMasterSource,
        unit_of_work_factory: Callable[[AccountId], InstrumentArchiveUnitOfWork],
    ) -> None:
        """Bind a read-only provider source and transaction factory."""
        self._discover = DiscoverOwnerInstruments(source)
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        command: ArchiveOwnerInstrumentMasterCommand,
    ) -> ArchiveOwnerInstrumentMasterResult:
        """Fetch once and commit the complete daily archive as one unit."""
        discovery = await self._discover.execute(
            DiscoverOwnerInstrumentsCommand(
                definitions=command.definitions,
                market_date=command.market_date,
            )
        )
        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            write = await unit_of_work.instrument_archive.archive(discovery)
            await unit_of_work.commit()
        return ArchiveOwnerInstrumentMasterResult(
            provider=discovery.snapshot.provider,
            market_date=discovery.snapshot.market_date,
            content_sha256=discovery.snapshot.content_sha256,
            resolver_revision=discovery.resolver_revision,
            write=write,
        )


class GetArchivedInstrumentDiscovery:
    """Replay mapped cash and actual-futures facts for a provider date."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], InstrumentArchiveUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        *,
        account_id: AccountId,
        provider: str,
        market_date: date,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery:
        """Return one immutable archived resolution without changing state."""
        async with self._unit_of_work_factory(account_id) as unit_of_work:
            return await unit_of_work.instrument_archive.get(
                provider=provider,
                market_date=market_date,
                resolver_revision=resolver_revision,
            )
