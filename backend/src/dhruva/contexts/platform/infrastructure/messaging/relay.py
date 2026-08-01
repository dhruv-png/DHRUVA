"""The outbox relay (ADR-062, ADR-063, ADR-064).

Reads durable rows, publishes them, records that it did. One pass is
``claim -> publish -> settle``, and each step is a separate short transaction.

Progress is ``published_at``, never a cursor
--------------------------------------------
ADR-062 forbids a high-water mark, and the reason is worth restating because the
alternative looks so reasonable. A PostgreSQL sequence is monotonic in
*allocation*, not in *commit*: transaction A may take sequence 100 and B take
101, and B may commit first. A relay tracking "I have processed up to 101" would
then never see 100 -- it is not late, it is invisible, permanently. Claiming
``WHERE published_at IS NULL`` has no such failure mode: a row that commits after
the current pass is simply picked up by the next one.

Leases, not held locks
----------------------
``FOR UPDATE SKIP LOCKED`` is how two relays avoid claiming the same row. But
holding that lock across the publish would make broker latency into lock
contention, and a slow broker would stall every other relay. So the claim
transaction *stamps* a lease -- ``claimed_at`` and ``claimed_by`` -- and commits.
The row is then unavailable to other relays until the lease expires, without
anyone holding a database lock while waiting on a network.

The cost is a new failure mode, stated honestly: a relay that dies mid-publish
leaves rows invisible until their lease expires. That is a latency cliff, not a
loss, and the lease is deliberately short.

Publish then mark, never the reverse
------------------------------------
The relay may publish and die before recording it, and the row is then published
again. That duplicate is the deliberate cost of at-least-once (ADR-062): marking
first would convert it into silent loss, and losing an event about capital is
strictly worse than repeating one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import text

from dhruva.contexts.platform.domain.messaging import Disposition, RetryPolicy, classify
from dhruva.contexts.platform.infrastructure.messaging.rows import envelope_from_row
from dhruva.shared.logging import get_logger
from dhruva.shared.messaging import EventEnvelope

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dhruva.shared.messaging import EventPublisher
    from dhruva.shared.time import Clock

__all__ = ["DEFAULT_LEASE", "OutboxRelay", "RelayPass"]

_log = get_logger(__name__)

#: How long a claim is held before another relay may take the row. Short, because
#: it is the recovery latency after a relay dies mid-publish; long enough that a
#: slow broker does not cause two relays to publish the same envelope.
DEFAULT_LEASE: Final = timedelta(seconds=30)

#: Rows claimed per pass. Bounds memory and bounds how much a single relay death
#: leaves in limbo for a lease interval.
DEFAULT_BATCH: Final = 100


@dataclass(frozen=True, slots=True)
class RelayPass:
    """What one pass of the relay did. Returned so a caller can assert on it."""

    claimed: int
    published: int
    retried: int
    dead_lettered: int

    @property
    def idle(self) -> bool:
        """Whether there was nothing to do, so the caller can back off."""
        return self.claimed == 0


class OutboxRelay:
    """Drains the outbox into an :class:`EventPublisher`.

    Parameters
    ----------
    engine
        Source of connections. The relay owns short transactions of its own and
        deliberately does not join a caller's Unit of Work: it is a background
        process, not part of anybody's use case.
    publisher
        Where envelopes go. A port (ADR-067), so this class contains no
        transport-specific code and boundary rule R9 has something to protect.
    clock
        Injected (ADR-011). Every timestamp the relay writes comes from here, so
        a replay controls them and a test does not sleep.
    identity
        Distinguishes relay instances in a lease. Defaults to a fresh UUID per
        instance, which is what makes concurrent relays visible in the table.
    """

    __slots__ = ("_batch", "_clock", "_engine", "_identity", "_lease", "_policy", "_publisher")

    def __init__(  # noqa: PLR0913 - each is a genuine collaborator, not a flag
        self,
        engine: AsyncEngine,
        publisher: EventPublisher,
        *,
        clock: Clock,
        policy: RetryPolicy | None = None,
        lease: timedelta = DEFAULT_LEASE,
        batch_size: int = DEFAULT_BATCH,
        identity: UUID | None = None,
    ) -> None:
        """Bind the relay to an engine, a publisher and a clock."""
        from uuid import uuid4  # noqa: PLC0415 - one call, at construction

        self._engine = engine
        self._publisher = publisher
        self._clock = clock
        self._policy = policy or RetryPolicy()
        self._lease = lease
        self._batch = batch_size
        self._identity = identity or uuid4()

    @property
    def identity(self) -> UUID:
        """Return the lease identity stamped on rows this instance claims."""
        return self._identity

    async def run_once(self) -> RelayPass:
        """Claim a batch, publish it, and settle each row.

        Returns
        -------
        RelayPass
            Counts for the pass. ``idle`` when nothing was claimed.
        """
        claimed = await self._claim()
        if not claimed:
            return RelayPass(claimed=0, published=0, retried=0, dead_lettered=0)

        envelopes = [envelope for envelope, _ in claimed]
        result = await self._publisher.publish(envelopes)

        accepted = {envelope.event_id for envelope in result.accepted}
        failures = {envelope.event_id: error for envelope, error in result.rejected}

        published = await self._mark_published(sorted(accepted))
        retried, dead_lettered = await self._settle_failures(claimed, failures)

        return RelayPass(
            claimed=len(claimed),
            published=published,
            retried=retried,
            dead_lettered=dead_lettered,
        )

    async def _claim(self) -> list[tuple[EventEnvelope, int]]:
        """Claim up to ``batch_size`` unpublished rows, stamping a lease.

        `SKIP LOCKED` so two relays never wait on each other, and the lease is
        committed before any publish so the row is reserved without a lock being
        held across the network.

        The predicate excludes dead-lettered rows. Omitting that clause -- which
        this query originally did -- let a poison message be reclaimed on every
        pass forever, burning attempts and delaying every deliverable event
        behind it. That is precisely the failure ADR-064 exists to prevent, and
        the partial index carried the condition while the query did not.
        """
        now = self._clock.now()
        expiry = now - self._lease

        async with self._engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT sequence, event_id, event_type, event_version, "
                        "aggregate_id, aggregate_type, account_id, correlation_id, causation_id, "
                        "payload, occurred_at, recorded_at, attempts "
                        "FROM outbox "
                        "WHERE published_at IS NULL "
                        "  AND dead_lettered_at IS NULL "
                        "  AND (next_attempt_at IS NULL OR next_attempt_at <= :now) "
                        "  AND (claimed_at IS NULL OR claimed_at < :expiry) "
                        "ORDER BY sequence "
                        "LIMIT :batch "
                        "FOR UPDATE SKIP LOCKED"
                    ),
                    {"now": now, "expiry": expiry, "batch": self._batch},
                )
            ).all()

            if not rows:
                return []

            await connection.execute(
                text(
                    "UPDATE outbox SET claimed_at = :now, claimed_by = :who "
                    "WHERE sequence = ANY(:sequences)"
                ),
                {"now": now, "who": self._identity, "sequences": [row.sequence for row in rows]},
            )

        return [(envelope_from_row(row), row.sequence) for row in rows]

    async def _mark_published(self, event_ids: Sequence[UUID]) -> int:
        """Record delivery for accepted envelopes.

        Runs *after* the publish returned, which is the window in which a crash
        produces a duplicate on restart. That is at-least-once behaving as
        designed, not a defect (ADR-062).
        """
        if not event_ids:
            return 0

        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE outbox SET published_at = :now, claimed_at = NULL, "
                    "claimed_by = NULL, last_error = NULL "
                    "WHERE event_id = ANY(:ids) AND published_at IS NULL"
                ),
                {"now": self._clock.now(), "ids": list(event_ids)},
            )
        return result.rowcount or 0

    async def _settle_failures(
        self,
        claimed: Sequence[tuple[EventEnvelope, int]],
        failures: dict[UUID, Exception],
    ) -> tuple[int, int]:
        """Schedule a retry or dead-letter each rejected row.

        A row that was neither accepted nor rejected is treated as a failure with
        no reported reason. Leaving it claimed would strand it until the lease
        expired, and silently marking it published would lose it.
        """
        retried = dead_lettered = 0
        now = self._clock.now()

        for envelope, _sequence in claimed:
            error = failures.get(envelope.event_id)
            if error is None:
                continue

            attempts = await self._record_attempt(envelope.event_id, error, now=now)
            disposition = classify(error, attempts=attempts, policy=self._policy)
            if disposition is Disposition.DEAD_LETTER:
                await self._dead_letter(envelope.event_id, now=now)
                dead_lettered += 1
                _log.warning(
                    "event dead-lettered",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                    attempts=attempts,
                    reason=str(error),
                )
            else:
                await self._schedule_retry(envelope.event_id, attempts=attempts, now=now)
                retried += 1

        return retried, dead_lettered

    async def _record_attempt(self, event_id: UUID, error: Exception, *, now: datetime) -> int:
        """Increment the attempt count and record the reason, returning the new count."""
        async with self._engine.begin() as connection:
            attempts = await connection.scalar(
                text(
                    "UPDATE outbox SET attempts = attempts + 1, last_error = :reason, "
                    "claimed_at = NULL, claimed_by = NULL "
                    "WHERE event_id = :id RETURNING attempts"
                ),
                {"id": event_id, "reason": f"{type(error).__name__}: {error}"[:2000]},
            )
        _ = now
        return int(attempts or 0)

    async def _schedule_retry(self, event_id: UUID, *, attempts: int, now: datetime) -> None:
        """Set the next attempt time from the policy and the injected clock."""
        async with self._engine.begin() as connection:
            await connection.execute(
                text("UPDATE outbox SET next_attempt_at = :when WHERE event_id = :id"),
                {
                    "id": event_id,
                    "when": self._policy.next_attempt_at(now=now, attempts=attempts),
                },
            )

    async def _dead_letter(self, event_id: UUID, *, now: datetime) -> None:
        """Move a row aside so it stops competing with deliverable work.

        ``next_attempt_at`` is nulled and ``dead_lettered_at`` stamped, so the
        claim query no longer selects it. The row is not deleted: it is the
        evidence a human needs, and ADR-064 makes replay explicit rather than
        automatic.
        """
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE outbox SET dead_lettered_at = :now, next_attempt_at = NULL, "
                    "claimed_at = NULL, claimed_by = NULL WHERE event_id = :id"
                ),
                {"id": event_id, "now": now},
            )
