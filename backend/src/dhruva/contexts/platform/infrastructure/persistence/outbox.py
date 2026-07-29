"""The transactional outbox.

Domain events must not be lost if the process dies between committing a change
and publishing the event it caused. Publishing inside the transaction is not an
option either -- a consumer could then observe an event for work that later rolls
back (ADR-053).

The outbox resolves both: the event row is written **in the same transaction as
the aggregate change**, so it commits or rolls back with it, and a separate
drainer publishes it afterwards.

Guarantees
----------
**Ordering.** Rows carry a monotonic ``sequence`` from a database sequence, so
the drainer can publish in commit order. Ordering is guaranteed *per aggregate*,
which is what consumers actually need -- two events about the same instrument
arrive in the order they happened. Strict global ordering across aggregates is
**not** guaranteed and should not be relied on: enforcing it would serialise
every write in the platform.

**Retry.** A failed publication increments ``attempts`` and sets
``next_attempt_at`` with exponential backoff. Rows are never dropped for failing;
they age into a dead-letter state that alerts rather than disappearing.

**Duplicate protection.** Delivery is **at-least-once**, not exactly-once. A
crash between publishing and marking published republishes on recovery.
Exactly-once delivery across a process boundary is not achievable without
distributed transactions, and the honest engineering answer is at-least-once
delivery plus **idempotent consumers** -- which together give
*effectively-once processing*. Every event carries a stable ``event_id`` for
consumers to deduplicate on, and S05's consumers must do so.

**Failure recovery.** An unpublished row is simply one where ``published_at`` is
null. A drainer restarting picks up exactly where it stopped, because the state
lives in the database rather than in the drainer.

What this module owns
---------------------
The table and the write path. The drainer belongs to S05, which owns the bus it
publishes to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from dhruva.contexts.platform.infrastructure.persistence.models import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.shared.events import DomainEvent

__all__ = ["OutboxRow", "OutboxWriter"]

#: Rows that have failed this many times stop being retried and are alerted on.
#: Retrying forever turns a poison message into an infinite loop that also delays
#: every well-behaved event behind it.
MAX_DELIVERY_ATTEMPTS = 12


class OutboxRow(Base):
    """One pending or delivered domain event.

    Retained after publication rather than deleted. The rows are the audit trail
    of what the platform emitted and when, which matters for the same reason
    ADR-014's order ledger does -- and a nightly job prunes them once they are
    older than the retention window.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_unpublished", "published_at", "next_attempt_at"),
        Index("ix_outbox_aggregate", "aggregate_id", "sequence"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=False, unique=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    account_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # server_default as well as default: the migration creates the column with
    # a DDL default, and a model that declares only the ORM-side one makes
    # autogenerate propose dropping it. See models.py for the full reasoning.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutboxWriter:
    """Stages domain events into the outbox, inside the caller's transaction.

    Deliberately does **not** commit. It is given the Unit of Work's session, so
    the event row and the aggregate change share one transaction and one fate.
    """

    __slots__ = ("_serialise", "_session")

    def __init__(
        self,
        session: AsyncSession,
        serialise: Any = None,
    ) -> None:
        """Bind the writer to a session and an optional payload serialiser."""
        self._session = session
        self._serialise = serialise or _default_payload

    def stage(
        self,
        event: DomainEvent,
        *,
        recorded_at: datetime,
        aggregate_id: UUID | None = None,
        account_id: UUID | None = None,
    ) -> None:
        """Write the event row into the current transaction.

        Parameters
        ----------
        event
            The fact that occurred.
        recorded_at
            When the platform learned it, supplied by the caller from an injected
            clock rather than read here (ADR-011). Distinct from
            ``event.occurred_at``, which is when it became true -- conflating the
            two is how lookahead bias enters a backtest (ADR-007).
        aggregate_id
            Used for per-aggregate ordering. Optional, because not every event
            belongs to an aggregate.
        account_id
            Present from day one so no row is retrofitted at S44 (ADR-004).
        """
        self._session.add(
            OutboxRow(
                event_id=event.event_id,
                event_type=event.event_type,
                aggregate_id=aggregate_id,
                account_id=account_id,
                payload=self._serialise(event),
                occurred_at=event.occurred_at,
                recorded_at=recorded_at,
                published_at=None,
                next_attempt_at=None,
                attempts=0,
                last_error=None,
            )
        )


def _default_payload(event: DomainEvent) -> str:
    """Serialise an event's fields to JSON.

    Deliberately simple, and deliberately not a general object serialiser. S05
    replaces this with the versioned envelope encoder once the event schema
    matters for compatibility; until then a readable JSON body is worth more than
    a clever one.
    """
    fields = {
        name: _encode(getattr(event, name))
        for name in getattr(event, "__dataclass_fields__", {})
        if name != "event_id"
    }
    return json.dumps(fields, sort_keys=True)


def _encode(value: object) -> object:
    """Convert a field to something JSON can carry, without losing precision."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str | int | bool | type(None)):
        return value
    return str(value)
