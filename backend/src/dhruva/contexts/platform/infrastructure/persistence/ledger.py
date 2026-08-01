"""The consumer idempotency ledger (ADR-065).

At-least-once delivery (ADR-062) means every consumer will eventually see a
duplicate. Effectively-once *processing* is what a trading platform actually
needs, and where the deduplication check lives is what decides whether it works.

It lives here, in the consumer's own transaction. The ledger row is written
alongside the side effect, so a duplicate violates the primary key, the whole
unit of work rolls back, and the side effect does not happen twice.

Why there is no ``already_processed()`` query
---------------------------------------------
It is the obvious convenience and it would be a trap. A check followed by a
write is two statements with a gap, and two workers in the same consumer group
can both pass the check before either writes. What makes this mechanism sound is
that the *database* decides, once, at the moment of writing -- so the only API
offered is the one that cannot race.

The cost is real and worth naming: a duplicate is discovered *after* the
consumer has done its work, and that work is rolled back. Wasted effort, never
wasted correctness. A consumer for which that cost is genuinely too high needs a
cheaper filter in front of this, and the filter would be an optimisation whose
failure mode is a wasted transaction rather than a repeated side effect.

What this does not protect
--------------------------
A side effect that is not part of the transaction -- an order sent to a broker.
ADR-015's client-generated idempotency key is what protects that, and the two
layers are deliberate: a duplicate event and a duplicate order need different
remedies.
"""

from __future__ import annotations

# `datetime` is imported at runtime, not under TYPE_CHECKING. SQLAlchemy resolves
# `Mapped[datetime]` when the class below is defined, and a type-checking-only
# import leaves it unresolvable -- models.py imports `date` for the same reason.
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import DateTime, Index, String, delete
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from dhruva.contexts.platform.infrastructure.persistence.models import Base
from dhruva.shared.errors import ConfigurationError, ConflictError

if TYPE_CHECKING:
    from sqlalchemy import Delete
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["LIVE_RUN_ID", "DuplicateEventError", "IdempotencyLedger", "ProcessedEventRow"]

#: The run identifier every live consumer uses.
#:
#: A constant rather than a nullable column. ``run_id`` is part of the primary
#: key, and a NULL in a primary key is not permitted -- but more importantly, a
#: nullable discriminator would make "live" the absence of a value, so a replay
#: that forgot to set one would silently deduplicate against production history.
#: An explicit sentinel makes live a value like any other, and one that cannot be
#: produced by accident: ``uuid4`` will not generate it.
LIVE_RUN_ID: Final = UUID(int=0)


class DuplicateEventError(ConflictError):
    """Raised when an event has already been processed by this group in this run.

    Not an error in the sense that something went wrong. It is the mechanism
    working: at-least-once redelivered an event, and the ledger refused the
    second attempt. The caller rolls back and acknowledges the delivery, because
    the work was already done.
    """


class ProcessedEventRow(Base):
    """One event, processed once, by one consumer group, in one run."""

    __tablename__ = "processed_event"
    __table_args__ = (
        Index("ix_processed_event_processed_at", "processed_at"),
        Index("ix_processed_event_run", "run_id"),
    )

    consumer_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyLedger:
    """Records that a consumer group has processed an event, inside its transaction.

    Parameters
    ----------
    session
        The consumer's session -- the same one its side effects are written
        through. Deliberately not a session of its own: a ledger in a separate
        transaction is a ledger that can commit while the work rolls back, which
        is the failure it exists to prevent.
    consumer_group
        Which logical consumer this is. Risk and reporting each process the same
        event once, so the group is part of the identity.
    run_id
        One live session or one replay run. Defaults to :data:`LIVE_RUN_ID`;
        a replay allocates a fresh one so that re-running a backtest processes
        the history again rather than finding it all already done (ADR-065).
    """

    __slots__ = ("_consumer_group", "_run_id", "_session")

    def __init__(
        self,
        session: AsyncSession,
        *,
        consumer_group: str,
        run_id: UUID = LIVE_RUN_ID,
    ) -> None:
        """Bind the ledger to a session, a consumer group and a run."""
        if not consumer_group:
            raise ConfigurationError(
                "a consumer group name is required; an unnamed group would share "
                "one ledger with every other consumer and suppress their events"
            )
        self._session = session
        self._consumer_group = consumer_group
        self._run_id = run_id

    @property
    def consumer_group(self) -> str:
        """Return the consumer group this ledger records for."""
        return self._consumer_group

    @property
    def run_id(self) -> UUID:
        """Return the run this ledger is scoped to."""
        return self._run_id

    async def record(self, event_id: UUID, *, processed_at: datetime) -> None:
        """Claim an event as processed, or refuse it as a duplicate.

        Parameters
        ----------
        event_id
            The identity the producer stamped once and every redelivery carries
            unchanged (ADR-061).
        processed_at
            From an injected clock (ADR-011), never read here, so a replay
            controls it and the retention job is deterministic in a test.

        Raises
        ------
        DuplicateEventError
            If this group has already processed this event in this run. The
            caller must roll back: PostgreSQL has aborted the transaction, and
            the side effects that accompanied this call are gone with it, which
            is the entire point.

        Notes
        -----
        ``flush`` rather than ``commit``. The ledger does not own the
        transaction -- the caller's Unit of Work does (ADR-053) -- but the
        conflict has to surface *here*, while the caller can still tell which
        event caused it. Deferring to commit would report the violation with no
        indication of which of a batch's events was the duplicate.
        """
        self._session.add(
            ProcessedEventRow(
                consumer_group=self._consumer_group,
                event_id=event_id,
                run_id=self._run_id,
                processed_at=processed_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise DuplicateEventError(
                "event has already been processed by this consumer group in this run",
                consumer_group=self._consumer_group,
                event_id=str(event_id),
                run_id=str(self._run_id),
            ) from error

    async def prune_before(self, cutoff: datetime) -> int:
        """Delete ledger rows processed before ``cutoff``, returning how many.

        Retention, run on a schedule (ADR-065). The window must comfortably
        exceed the transport's own retention: pruning a row while the transport
        could still redeliver the event it describes converts the ledger from a
        guard into a source of duplicates.
        """
        return await self._delete(
            delete(ProcessedEventRow).where(ProcessedEventRow.processed_at < cutoff)
        )

    async def prune_run(self) -> int:
        """Delete every row belonging to this ledger's run, returning how many.

        A replay prunes its own run on completion (ADR-065). Without it the
        ledger accumulates one row per event per backtest, and the next run of
        the same backtest -- which allocates a fresh ``run_id`` -- would still be
        correct but would be reading a table nobody had cleaned since the first.
        """
        return await self._delete(
            delete(ProcessedEventRow).where(ProcessedEventRow.run_id == self._run_id)
        )

    async def _delete(self, statement: Delete) -> int:
        """Execute a delete and report how many rows it removed.

        The count comes off the driver's cursor rather than the typed result:
        SQLAlchemy's ``Result`` is generic over the *rows* a statement returns,
        and a DELETE returns none, so ``rowcount`` is a property of the cursor
        underneath. Narrowed here, once, rather than at each call site.
        """
        result = await self._session.execute(statement)
        return int(result.rowcount)  # type: ignore[attr-defined]  # CursorResult, not Result
