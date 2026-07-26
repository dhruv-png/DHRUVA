"""The SQLAlchemy Unit of Work.

Owns transaction lifetime (ADR-053). One use case, one Unit of Work, one
transaction. Repositories participate; they never commit.

Four behaviours are load-bearing and each is tested:

* **Rollback is the default.** Leaving the block without an explicit
  :meth:`commit` rolls back. The alternative -- committing on clean exit -- means
  a use case that returns early commits partial work, silently.
* **Nesting is refused, not joined.** Joining an outer transaction would make an
  inner ``commit()`` a no-op that appears to succeed, and the caller could not
  tell.
* **Events publish after commit, never inside it.** Publishing inside means a
  consumer can observe an event for a transaction that later rolls back.
* **The session is always disposed**, on every path including an exception during
  commit itself.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.shared.events import DomainEvent

__all__ = ["SqlAlchemyUnitOfWork"]

_log = get_logger(__name__)


async def _discard_events(events: Sequence[DomainEvent]) -> None:
    """Default publisher: record and drop.

    Used until S05 provides a real bus. Logs at debug so that events raised
    before the bus exists are visible in development rather than silently lost.
    """
    if events:
        _log.debug("domain events discarded; no publisher configured", count=len(events))


class SqlAlchemyUnitOfWork:
    """A transaction boundary over an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

    Examples
    --------
    ::

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            instrument = await uow.session.get(InstrumentModel, identifier)
            ...
            await uow.commit()
    """

    __slots__ = ("_committed", "_events", "_publish", "_session", "_session_factory")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publish: Callable[[Sequence[DomainEvent]], Awaitable[None]] | None = None,
    ) -> None:
        """Create a Unit of Work bound to a session factory.

        Parameters
        ----------
        session_factory
            Produces one session per Unit of Work. Never shared across tasks
            (ADR-056).
        publish
            Called with the collected events **after** a successful commit.
            Defaults to discarding them, which is correct until S05.
        """
        self._session_factory = session_factory
        self._publish = publish or _discard_events
        self._session: AsyncSession | None = None
        self._events: list[DomainEvent] = []
        self._committed = False

    @property
    def session(self) -> AsyncSession:
        """Return the active session.

        Raises
        ------
        InvariantViolation
            If accessed outside the ``async with`` block. A session obtained
            outside a transaction boundary has no defined lifetime, which is the
            ambient-session problem ADR-056 exists to prevent.
        """
        if self._session is None:
            msg = "unit of work is not active; use it as an async context manager"
            raise InvariantViolation(msg)
        return self._session

    def add_event(self, event: DomainEvent) -> None:
        """Stage a domain event for publication after commit.

        Staged rather than published, because an event published inside the
        transaction can be observed for work that later rolls back.
        """
        self._events.append(event)

    @property
    def staged_events(self) -> tuple[DomainEvent, ...]:
        """Return the events awaiting publication. Empty after they are published."""
        return tuple(self._events)

    async def __aenter__(self) -> Self:
        """Open a session and begin a transaction.

        Raises
        ------
        InvariantViolation
            If this Unit of Work is already active. Nesting is refused rather
            than joined (ADR-053).
        """
        if self._session is not None:
            msg = (
                "unit of work is already active; nesting is refused rather than joined, "
                "because an inner commit would appear to succeed while doing nothing"
            )
            raise InvariantViolation(msg)
        self._session = self._session_factory()
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless committed, then always dispose of the session."""
        session = self._session
        if session is None:  # pragma: no cover - unreachable via the context manager
            return
        try:
            if not self._committed:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
            self._events.clear()
            self._committed = False

    async def commit(self) -> None:
        """Commit the transaction, then publish staged events.

        Notes
        -----
        Events publish only after the commit returns. If the commit raises, the
        events are discarded by ``__aexit__`` along with everything else -- which
        is the point: nothing observable happened, so nothing should be observed.
        """
        await self.session.commit()
        self._committed = True

        events = tuple(self._events)
        self._events.clear()
        if events:
            await self._publish(events)

    async def rollback(self) -> None:
        """Discard everything staged in this transaction, including events."""
        await self.session.rollback()
        self._events.clear()
