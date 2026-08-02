"""Persistence contracts used by reference-data application services."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.contexts.reference.domain.watchlist import (
        InstrumentIdentityRevision,
        WatchlistInstrument,
        WatchlistMembershipRevision,
    )
    from dhruva.shared.identity import AccountId

__all__ = ["ReferenceStore", "ReferenceUnitOfWork"]


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
