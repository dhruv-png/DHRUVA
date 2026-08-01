"""Reading and re-queueing dead letters (ADR-064).

The dead-letter queue is not a second table. A dead-lettered row stays in the
outbox with ``dead_lettered_at`` stamped, so it keeps its identity, its attempt
history and its last error together, and "what is dead-lettered?" is one
predicate rather than a join. This module is the read and re-queue side of that.

Re-queueing is explicit and never automatic. ADR-064 chose that deliberately: a
queue that retries a poison message forever is a queue that delays every
well-behaved event behind it, and a queue that discards one loses an event about
capital. Moving it aside and waiting for a human is the third option, and the
waiting is the point.

The decision itself lives in :mod:`dhruva.contexts.platform.domain.messaging`,
not here. This module reads state, asks, and writes the answer down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from dhruva.contexts.platform.domain.messaging.dead_letters import (
    RequeueRefusal,
    RequeueVerdict,
    may_requeue,
)
from dhruva.shared.errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["DeadLetter", "DeadLetterStore", "RequeueReport"]

#: Rows returned by one `list` call unless the caller says otherwise. An operator
#: reading a dead-letter queue is looking for a pattern, not paging through
#: thousands; an unbounded default would be a query nobody meant to run.
DEFAULT_LIMIT = 50


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One event that will not be delivered without intervention.

    Carries the last error and the attempt count because those are what an
    operator actually decides on. A listing that showed only identifiers would
    send them back to the database to find out what happened.
    """

    event_id: UUID
    event_type: str
    aggregate_type: str | None
    aggregate_id: UUID | None
    attempts: int
    last_error: str | None
    occurred_at: datetime
    recorded_at: datetime
    dead_lettered_at: datetime


@dataclass(frozen=True, slots=True)
class RequeueReport:
    """What a bulk re-queue did, per outcome rather than as a single number."""

    requeued: int
    refused: dict[RequeueRefusal, int]


class DeadLetterStore:
    """Reads and re-queues dead-lettered outbox rows.

    Parameters
    ----------
    engine
        Source of connections. Short transactions of its own: this is an
        operator's tool, not part of anybody's use case, and it must not join a
        Unit of Work that could roll its decision back.
    """

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind the store to an engine."""
        self._engine = engine

    async def count(self) -> int:
        """Return how many events are currently dead-lettered."""
        async with self._engine.connect() as connection:
            return int(
                await connection.scalar(
                    text("SELECT count(*) FROM outbox WHERE dead_lettered_at IS NOT NULL")
                )
                or 0
            )

    async def list(
        self, *, limit: int = DEFAULT_LIMIT, event_type: str | None = None
    ) -> Sequence[DeadLetter]:
        """Return dead letters, oldest first.

        Oldest first because a dead-letter queue is read to find the *first*
        thing that went wrong. Newest-first would put the consequences at the top
        and the cause on the last page.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive", limit=limit)

        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT event_id, event_type, aggregate_type, aggregate_id, attempts, "
                        "last_error, occurred_at, recorded_at, dead_lettered_at "
                        "FROM outbox "
                        "WHERE dead_lettered_at IS NOT NULL "
                        # asyncpg is a binary protocol and infers a parameter's type from its use.
                        # A bare `:event_type IS NULL` gives it nothing to infer from and it
                        # refuses the statement outright -- so the cast is load-bearing, not
                        # decoration, and removing it breaks every filtered listing.
                        "  AND (CAST(:event_type AS text) IS NULL "
                        "       OR event_type = CAST(:event_type AS text)) "
                        "ORDER BY dead_lettered_at, sequence "
                        "LIMIT :limit"
                    ),
                    {"event_type": event_type, "limit": limit},
                )
            ).all()

        return [
            DeadLetter(
                event_id=row.event_id,
                event_type=row.event_type,
                aggregate_type=row.aggregate_type,
                aggregate_id=row.aggregate_id,
                attempts=row.attempts,
                last_error=row.last_error,
                occurred_at=row.occurred_at,
                recorded_at=row.recorded_at,
                dead_lettered_at=row.dead_lettered_at,
            )
            for row in rows
        ]

    async def requeue(self, event_id: UUID) -> RequeueVerdict:
        """Return one event to the delivery queue, or refuse and say why.

        The attempt count is reset. An operator re-queueing a dead letter has
        decided the cause is fixed, and a row returning with eleven attempts
        already spent would dead-letter again on the first hiccup -- which looks
        exactly like the fix not working.

        The whole read-decide-write runs in one transaction with ``FOR UPDATE``,
        so two operators re-queueing the same event at the same moment cannot
        both see it as dead-lettered and both reset it.
        """
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT published_at, dead_lettered_at FROM outbox "
                        "WHERE event_id = :id FOR UPDATE"
                    ),
                    {"id": event_id},
                )
            ).one_or_none()

            if row is None:
                raise NotFoundError("no outbox row with this event id", event_id=str(event_id))

            verdict = may_requeue(
                published=row.published_at is not None,
                dead_lettered=row.dead_lettered_at is not None,
            )
            if verdict.permitted:
                await connection.execute(_REQUEUE, {"id": event_id})

        return verdict

    async def requeue_all(
        self, *, event_type: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> RequeueReport:
        """Re-queue up to ``limit`` dead letters, reporting each outcome.

        Bounded rather than unbounded. Returning ten thousand poison messages to
        a queue in one command is an outage, and an operator who wants that can
        run the command again.
        """
        letters = await self.list(limit=limit, event_type=event_type)

        requeued = 0
        refused: dict[RequeueRefusal, int] = {}
        for letter in letters:
            verdict = await self.requeue(letter.event_id)
            if verdict.permitted:
                requeued += 1
            elif verdict.refusal is not None:
                refused[verdict.refusal] = refused.get(verdict.refusal, 0) + 1

        return RequeueReport(requeued=requeued, refused=refused)


#: Clearing `last_error` as well is deliberate: a stale error message beside a
#: fresh attempt is the kind of detail that sends an operator chasing a failure
#: that already happened.
_REQUEUE = text(
    "UPDATE outbox SET dead_lettered_at = NULL, attempts = 0, next_attempt_at = NULL, "
    "last_error = NULL, claimed_at = NULL, claimed_by = NULL "
    "WHERE event_id = :id"
)
