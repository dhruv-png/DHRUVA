"""Replay as an :class:`EventStream` over the outbox (ADR-067, ADR-069).

A peer of the Redis adapter, not a lesser version of it. A consumer written
against the port cannot tell which one it has, which is the whole of ADR-010's
promise that one body of strategy code serves backtest, paper and live -- and it
is enforced by the same conformance suite both must pass unchanged.

``as_of`` is structural, not a filter
--------------------------------------
The bound lives in the ``WHERE`` clause and the constructor. A replay is
therefore *incapable* of returning an event the platform had not yet learned,
rather than merely unlikely to.

ADR-069 is explicit about why the distinction matters. A replay selecting on
``occurred_at`` alone hands a strategy a correction that arrived days later, and
the backtest then trades on knowledge it did not have. The result is not merely
wrong, it is **optimistically** wrong -- which is the direction that gets capital
committed. A filter a caller may forget is not protection; a constructor
parameter enforced in the query is.

Total order, and why there is no type filter
---------------------------------------------
Live delivery guarantees ordering per aggregate; replay guarantees a *total*
order (ADR-062), and it gets one by reading every row in ``sequence`` order.
Offering an aggregate-type filter here would quietly break that: a consumer
reading a subset would see a different interleaving from one reading everything,
and two backtests of the same strategy would disagree for a reason nobody could
see. A consumer that wants a subset filters what it is handed.

Nothing here sleeps
-------------------
There is no polling and no wall-clock wait. ``read`` returns what is there and
returns immediately, so a year of history replays at the speed of the database
rather than at the speed of the year (ADR-069).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.messaging.rows import envelope_from_row
from dhruva.shared.errors import ValidationError
from dhruva.shared.messaging import Delivery, EventEnvelope

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dhruva.shared.messaging import AckToken

__all__ = ["OutboxReplayStream", "ReplayAck"]

#: The columns an envelope is rebuilt from. Named once so the two queries below
#: cannot disagree about what a row contains.
_COLUMNS = (
    "sequence, event_id, event_type, event_version, aggregate_id, aggregate_type, "
    "account_id, correlation_id, causation_id, payload, occurred_at, recorded_at"
)


@dataclass(frozen=True, slots=True)
class ReplayAck:
    """The opaque token acknowledging one replayed delivery.

    Carries the row's ordinal, and a consumer must not read it. One that did
    could branch on it, and would then be a consumer that behaves differently in
    a backtest from in production -- which is exactly what ADR-067 forbids and
    what the conformance suite exists to catch.
    """

    sequence: int


class OutboxReplayStream:
    """Replays persisted events as an :class:`EventStream`.

    Parameters
    ----------
    engine
        Source of connections. Short read-only transactions of its own: a replay
        is not part of anybody's use case and must not join a Unit of Work whose
        rollback would discard its progress.
    as_of
        The instant the replay pretends to be. No event recorded after it is
        reachable through this object at all. Must be timezone-aware (ADR-006):
        a naive bound has no defined position in the sequence of events, and one
        interpreted in the wrong zone silently admits hours of the future.
    supported_versions
        Envelope schema versions this consumer understands (ADR-061). An
        envelope declaring anything else raises rather than being parsed on the
        assumption that the payload is close enough.

    Notes
    -----
    Progress is a cursor over ``sequence``, held in memory. A replay is one pass
    over history by one process; persisting the cursor would make a run resumable
    and would also make two runs of the same backtest produce different results
    depending on where the first one stopped. ADR-065's ledger is what makes a
    *re-run* meaningful, and it is keyed by ``run_id`` for that reason.
    """

    __slots__ = ("_as_of", "_cursor", "_engine", "_pending", "_supported_versions")

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        as_of: datetime,
        supported_versions: frozenset[int] | None = None,
    ) -> None:
        """Bind the replay to an engine and the instant it may not see past."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValidationError(
                "as_of must be timezone-aware; a naive bound has no defined "
                "position in the sequence of events (ADR-006)",
                as_of=as_of.isoformat(),
            )
        self._engine = engine
        self._as_of = as_of
        self._supported_versions = supported_versions
        self._cursor = 0
        self._pending: set[int] = set()

    @property
    def as_of(self) -> datetime:
        """Return the instant this replay may not see past."""
        return self._as_of

    async def read(self, *, limit: int) -> Sequence[Delivery]:
        """Return up to ``limit`` events in total order, or nothing when exhausted.

        Returns immediately. A replay that waited for more history would never
        finish, because the history it reads is not being written.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive", limit=limit)

        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM outbox "  # noqa: S608 - a module constant, no input
                        "WHERE recorded_at <= :as_of AND sequence > :cursor "
                        "ORDER BY sequence LIMIT :limit"
                    ),
                    {"as_of": self._as_of, "cursor": self._cursor, "limit": limit},
                )
            ).all()

        deliveries = [self._to_delivery(row) for row in rows]
        if rows:
            self._cursor = rows[-1].sequence
            self._pending.update(row.sequence for row in rows)
        return deliveries

    async def acknowledge(self, tokens: Sequence[AckToken]) -> int:
        """Confirm deliveries, returning how many were actually outstanding.

        A repeated acknowledgement counts zero rather than one, exactly as the
        Redis adapter's does. Nothing is written: the outbox row is the producer's
        record and a replay must not edit history it is reading.
        """
        acknowledged = 0
        for token in tokens:
            if not isinstance(token, ReplayAck):
                raise ValidationError(
                    "acknowledge received a token this stream did not issue",
                    actual_type=type(token).__name__,
                )
            acknowledged += int(token.sequence in self._pending)
            self._pending.discard(token.sequence)
        return acknowledged

    async def pending(self) -> int:
        """Return how many deliveries are read but not yet acknowledged."""
        return len(self._pending)

    async def remaining(self) -> int:
        """Return how many events are still ahead of the cursor within ``as_of``.

        Not part of the port. A replay is finite and a caller wants a progress
        figure; live delivery has no such number, so offering one through the
        port would be a fact a consumer could branch on.
        """
        async with self._engine.connect() as connection:
            return int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM outbox "
                        "WHERE recorded_at <= :as_of AND sequence > :cursor"
                    ),
                    {"as_of": self._as_of, "cursor": self._cursor},
                )
                or 0
            )

    def _to_delivery(self, row: Any) -> Delivery:
        """Rebuild one delivery, refusing an envelope this consumer cannot read."""
        envelope = envelope_from_row(row)
        if self._supported_versions is not None:
            # Re-parsed through the canonical form so the version check is the
            # same one the live adapter applies -- a second implementation of
            # "which versions are acceptable" is a second thing to get wrong.
            envelope = EventEnvelope.from_json(
                envelope.to_json(), supported_versions=self._supported_versions
            )
        return Delivery(envelope=envelope, ack_token=ReplayAck(sequence=row.sequence))
