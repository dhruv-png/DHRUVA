"""Persistence contracts used by reference-data application services."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.contexts.reference.domain.corporate_actions import CorporateActionEvidence
    from dhruva.contexts.reference.domain.historical_universe import (
        HistoricalUniverseDefinition,
        HistoricalUniverseMembershipRevision,
        ResolvedHistoricalUniverse,
    )
    from dhruva.contexts.reference.domain.instrument_master import (
        ArchivedInstrumentDiscovery,
        InstrumentArchiveWrite,
        InstrumentDiscovery,
        InstrumentMasterSnapshot,
    )
    from dhruva.contexts.reference.domain.watchlist import (
        InstrumentIdentityRevision,
        WatchlistInstrument,
        WatchlistMembershipRevision,
    )
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "CorporateActionProvider",
    "HistoricalInstrumentLifecycleProvider",
    "HistoricalReferenceStore",
    "HistoricalReferenceUnitOfWork",
    "HistoricalUniverseProvider",
    "InstrumentArchiveStore",
    "InstrumentArchiveUnitOfWork",
    "InstrumentMasterSource",
    "ReferenceStore",
    "ReferenceUnitOfWork",
]


@runtime_checkable
class HistoricalUniverseProvider(Protocol):
    """Read a licensed/reviewed source's PIT membership revisions."""

    async def fetch_universe(
        self, *, universe_id: str, from_date: date, to_date: date
    ) -> tuple[HistoricalUniverseDefinition, tuple[HistoricalUniverseMembershipRevision, ...]]:
        """Return source facts without inventing absent constituents."""
        ...


@runtime_checkable
class CorporateActionProvider(Protocol):
    """Read revisioned corporate-action evidence for one instrument."""

    async def fetch_actions(
        self, *, instrument_id: InstrumentId, from_date: date, to_date: date
    ) -> tuple[CorporateActionEvidence, ...]:
        """Return only facts attributable to the provider."""
        ...


@runtime_checkable
class HistoricalInstrumentLifecycleProvider(Protocol):
    """Narrow capability for effective and known-at identity revisions."""

    async def fetch_identity_revisions(
        self, *, from_date: date, to_date: date
    ) -> tuple[InstrumentIdentityRevision, ...]:
        """Return symbol/mapping history without treating symbols as identity."""
        ...


@runtime_checkable
class InstrumentMasterSource(Protocol):
    """Read-only source of one immutable daily instrument master."""

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Retrieve and parse the provider master assigned to ``market_date``."""
        ...


@runtime_checkable
class InstrumentArchiveStore(Protocol):
    """Append and replay immutable daily provider discoveries; never commit."""

    async def archive(self, discovery: InstrumentDiscovery) -> InstrumentArchiveWrite:
        """Stage one snapshot and its versioned mappings idempotently."""
        ...

    async def get(
        self,
        *,
        provider: str,
        market_date: date,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery:
        """Replay one archived resolution or report that it is missing."""
        ...

    async def get_latest(
        self,
        *,
        provider: str,
        resolver_revision: str,
    ) -> ArchivedInstrumentDiscovery | None:
        """Replay the most recently archived resolution, or ``None`` if none exists."""
        ...


@runtime_checkable
class ReferenceStore(Protocol):
    """Append and query point-in-time reference revisions; never commit."""

    async def add_identity(self, revision: InstrumentIdentityRevision) -> bool:
        """Stage an identity revision; return ``False`` for an identical retry."""
        ...

    async def add_membership(self, revision: WatchlistMembershipRevision) -> bool:
        """Stage a membership revision; return ``False`` for an identical retry."""
        ...

    async def list_watchlist(
        self,
        account_id: AccountId,
        *,
        effective_on: date,
        known_at: datetime,
    ) -> tuple[WatchlistInstrument, ...]:
        """Return the active watchlist using only knowledge available at ``known_at``."""
        ...


@runtime_checkable
class HistoricalReferenceStore(Protocol):
    """Append/query contract for licensed historical integrity evidence."""

    async def add_historical_universe_definition(
        self, definition: HistoricalUniverseDefinition
    ) -> bool:
        """Stage one append-only universe definition revision."""
        ...

    async def add_historical_universe_membership(
        self, revision: HistoricalUniverseMembershipRevision
    ) -> bool:
        """Stage one append-only historical membership revision."""
        ...

    async def get_historical_universe(
        self,
        account_id: AccountId,
        *,
        universe_id: str,
        effective_on: date,
        known_at: datetime,
    ) -> ResolvedHistoricalUniverse:
        """Resolve membership and identity using no later knowledge."""
        ...

    async def add_corporate_action(self, action: CorporateActionEvidence) -> bool:
        """Stage one immutable corporate-action source revision."""
        ...

    async def list_corporate_actions(
        self,
        instrument_id: InstrumentId,
        *,
        effective_from: date,
        effective_to: date,
        known_at: datetime,
    ) -> tuple[CorporateActionEvidence, ...]:
        """Return effective actions observable by the knowledge cutoff."""
        ...


@runtime_checkable
class HistoricalReferenceUnitOfWork(Protocol):
    """Transaction boundary for historical reference and action evidence."""

    @property
    def reference(self) -> HistoricalReferenceStore:
        """Return the historical evidence store."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless committed."""
        ...

    async def commit(self) -> None:
        """Commit staged evidence."""
        ...

    async def rollback(self) -> None:
        """Discard staged evidence."""
        ...


@runtime_checkable
class ReferenceUnitOfWork(Protocol):
    """One transaction and the reference store participating in it."""

    @property
    def reference(self) -> ReferenceStore:
        """Return the reference store bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` succeeded."""
        ...

    async def commit(self) -> None:
        """Commit every staged reference revision."""
        ...

    async def rollback(self) -> None:
        """Discard every staged reference revision."""
        ...


@runtime_checkable
class InstrumentArchiveUnitOfWork(Protocol):
    """Transaction boundary dedicated to global provider-reference facts."""

    @property
    def instrument_archive(self) -> InstrumentArchiveStore:
        """Return the archive store bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` succeeded."""
        ...

    async def commit(self) -> None:
        """Commit every staged archive fact."""
        ...

    async def rollback(self) -> None:
        """Discard every staged archive fact."""
        ...
