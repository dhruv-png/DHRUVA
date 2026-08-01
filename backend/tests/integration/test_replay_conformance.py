"""Replay passes the same transport conformance suite (ADR-067, ADR-069).

Every assertion the Redis adapter passes, against an adapter that reads
persisted rows rather than a broker. Two transports passing one unchanged suite
is what "live and replay are peers" means operationally, and it is the only
thing standing between this project and a backtest that quietly stops matching
production.

Why the table is truncated with ``RESTART IDENTITY``
-----------------------------------------------------
``EventEnvelope.sequence`` is the platform's global ordinal, and today that
ordinal is the outbox primary key. The suite publishes envelopes declaring
sequences 1..n and asserts they arrive in that order, so the fixture must make
the assigned ordinals coincide with the declared ones. An ordinal that merely
continued from wherever the last test left off would make the suite's ordering
assertion depend on execution order.

That is a fixture concern and not a design one. Nothing in either adapter cares
what the ordinal *is* -- only that both report the same one, which they do
because the relay copies it from this same column before Redis stores it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.messaging import OutboxReplayStream
from dhruva.shared.messaging import encode_value
from tests.transport_conformance import Bus, TransportConformance

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

    from dhruva.shared.messaging import EventEnvelope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

#: Far enough ahead that every row this suite writes is inside the bound. The
#: bound itself is exercised in ``test_replay_stream.py``; here it must simply
#: not interfere with what the shared suite is asserting.
FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)

_INSERT = text(
    "INSERT INTO outbox (event_id, event_type, event_version, aggregate_type, "
    " aggregate_id, account_id, correlation_id, causation_id, payload, "
    " occurred_at, recorded_at, attempts) "
    "VALUES (:event_id, :event_type, :event_version, :aggregate_type, "
    " :aggregate_id, :account_id, :correlation_id, :causation_id, :payload, "
    " :occurred_at, :recorded_at, 0)"
)


class RecordedOutcome:
    """One batch's outcome, satisfying the :class:`PublishResult` protocol."""

    def __init__(
        self,
        accepted: Sequence[EventEnvelope],
        rejected: Sequence[tuple[EventEnvelope, Exception]],
    ) -> None:
        """Record what was accepted and what was refused."""
        self.accepted = accepted
        self.rejected = rejected


class OutboxWritingPublisher:
    """Writes envelopes into the outbox, satisfying :class:`EventPublisher`.

    Replay's producer is the persistence layer, not a transport, so this stands
    in for the Unit of Work that writes rows in production. It exists only to
    give the shared suite something to publish through: the suite's contract is
    stated in terms of a publisher and a stream, and for replay the publisher is
    "something wrote a row".
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Bind the publisher to an engine."""
        self._engine = engine

    async def publish(self, envelopes: Sequence[EventEnvelope]) -> RecordedOutcome:
        """Write each envelope as an outbox row, in the order given."""
        async with self._engine.begin() as connection:
            for envelope in envelopes:
                await connection.execute(_INSERT, _parameters(envelope))
        return RecordedOutcome(accepted=list(envelopes), rejected=[])


def _parameters(envelope: EventEnvelope) -> dict[str, object]:
    """Return the row values for an envelope.

    ``sequence`` is deliberately absent: the database assigns it, and that
    assignment *is* the envelope's ordinal.
    """
    return {
        "event_id": envelope.event_id,
        "event_type": envelope.event_type,
        "event_version": envelope.event_version,
        "aggregate_type": envelope.aggregate_type,
        "aggregate_id": envelope.aggregate_id,
        "account_id": envelope.account_id,
        "correlation_id": envelope.correlation_id,
        "causation_id": envelope.causation_id,
        "payload": json.dumps(
            {key: encode_value(value) for key, value in envelope.payload.items()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        "occurred_at": envelope.occurred_at,
        "recorded_at": envelope.recorded_at,
    }


class TestReplayTransportConformance(TransportConformance):
    """Every assertion inherited, none overridden.

    An override here would be replay declaring it is not a peer of live
    delivery, which is the drift ADR-067 exists to prevent. When this suite was
    first run against replay it failed exactly one assertion -- redelivery -- and
    failed it for a correct reason: the outbox records each event once, by
    constraint. That assertion moved to the Redis tests, where the transport
    that promises redelivery lives. The suite was scoped, not weakened.
    """

    @pytest_asyncio.fixture(loop_scope="session")
    async def bus(self, migrated: AsyncEngine) -> Bus:
        """Empty the outbox, reset its ordinal, and wire a publisher to a replay."""
        async with migrated.begin() as connection:
            await connection.execute(text("TRUNCATE outbox RESTART IDENTITY CASCADE"))
        return Bus(
            publisher=OutboxWritingPublisher(migrated),
            stream=OutboxReplayStream(migrated, as_of=FAR_FUTURE),
        )
