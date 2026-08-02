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
* **Events are staged into the outbox, inside the transaction** (AR-001b). They
  are not held in memory and published after commit: the window between a commit
  returning and an in-process publish completing is a crash window in which the
  change is durable and the event is not. Staging into the outbox closes it --
  the event row commits or rolls back with the work that caused it, and the S05
  relay delivers it afterwards from durable state.
* **The session is always disposed**, on every path including an exception during
  commit itself.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self
from uuid import UUID

from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.database.tenant_context import (
    SESSION_ACCOUNT_SETTING,
)
from dhruva.contexts.platform.infrastructure.persistence.outbox import OutboxWriter
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId
from dhruva.shared.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.shared.events import DomainEvent
    from dhruva.shared.time import Clock

__all__ = ["SqlAlchemyUnitOfWork"]

_log = get_logger(__name__)


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

    __slots__ = (
        "_account_id",
        "_clock",
        "_committed",
        "_events",
        "_session",
        "_session_factory",
    )

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
        account_id: AccountId | None = None,
    ) -> None:
        """Create a Unit of Work bound to a session factory.

        Parameters
        ----------
        session_factory
            Produces one session per Unit of Work. Never shared across tasks
            (ADR-056).
        clock
            Supplies ``recorded_at`` when an event is staged -- when the platform
            *learned* a fact, as distinct from when it became true (ADR-007).
            Injected rather than read from the wall clock (ADR-011) so a replay
            controls it, which is what makes the ``as_of`` bound of ADR-069
            enforceable. Defaults to :class:`SystemClock`.
        account_id
            Tenant scope for an account-owned transaction. When present, entry
            sets PostgreSQL's account context transaction-locally before any
            repository can be reached. Authentication transactions whose tenant
            is not known yet leave it absent.
        """
        from dhruva.shared.time import SystemClock  # noqa: PLC0415 - avoids an import cycle

        self._session_factory = session_factory
        self._clock = clock or SystemClock()
        self._account_id = account_id
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

    def add_event(
        self,
        event: DomainEvent,
        *,
        aggregate_id: UUID | None = None,
        aggregate_type: str | None = None,
        account_id: UUID | None = None,
    ) -> None:
        """Stage a domain event into the outbox, inside this transaction.

        The event row is written to the session, so it commits or rolls back with
        the aggregate change that caused it. Nothing is published here and
        nothing is published at commit; the S05 relay reads the outbox and
        delivers from durable state.

        Parameters
        ----------
        event
            The fact that occurred. Its ``occurred_at`` is when it became true;
            ``recorded_at`` is taken from the injected clock, because those two
            differ whenever data arrives late and conflating them is how
            lookahead bias enters a backtest (ADR-007).
        aggregate_id
            Which aggregate this concerns, used for per-aggregate ordering
            (ADR-062). Optional: not every event belongs to an aggregate.
        aggregate_type
            Which kind of aggregate. The stream routing key (ADR-062), stated
            rather than derived -- a routing value inferred from a naming
            convention sends events to a stream named after a guess.
        account_id
            Tenant scope, present from day one so no row is retrofitted (ADR-004).

        Notes
        -----
        This method previously appended to an in-memory list which was published
        after ``commit()`` returned. That left a window in which the transaction
        was durable and the event existed only in process memory, so a crash lost
        it -- the dual-write problem the outbox exists to solve, sitting beside
        the outbox. AR-001b closed it by making this method write to the outbox
        rather than to a list.

        The rollback semantics callers relied on are unchanged, and now hold more
        strongly: they are a property of the transaction rather than of a
        ``finally`` block that clears a list.
        """
        writer = OutboxWriter(self.session)
        writer.stage(
            event,
            recorded_at=self._clock.now(),
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            account_id=account_id,
        )
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
        if self._account_id is not None:
            await self._session.execute(
                text("SELECT set_config(:setting, :account_id, true)"),
                {
                    "setting": SESSION_ACCOUNT_SETTING,
                    "account_id": str(self._account_id.value),
                },
            )
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
        """Commit the transaction, including any staged outbox rows.

        Notes
        -----
        Nothing is published here. The staged events are already rows in this
        transaction, so committing makes them durable and the relay delivers them
        afterwards. If the commit raises, the rows go with it.

        This method used to publish in-process after the commit returned. A
        publisher failure then surfaced as an exception from ``commit()`` for work
        that had actually succeeded, and a caller that retried would apply the use
        case twice. Removing the publication removes that failure mode with it.
        """
        await self.session.commit()
        self._committed = True
        self._events.clear()

    async def rollback(self) -> None:
        """Discard everything staged in this transaction, including events."""
        await self.session.rollback()
        self._events.clear()
