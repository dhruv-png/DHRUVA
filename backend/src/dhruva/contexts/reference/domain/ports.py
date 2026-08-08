"""Persistence contracts used by reference-data application services."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import date, datetime

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
    from dhruva.shared.identity import AccountId

__all__ = [
    "InstrumentArchiveStore",
    "InstrumentArchiveUnitOfWork",
    "InstrumentMasterSource",
    "ReferenceStore",
    "ReferenceUnitOfWork",
]


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
