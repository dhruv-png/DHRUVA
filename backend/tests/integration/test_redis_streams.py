"""The Redis Streams adapter against a real Redis (ADR-002, ADR-067).

Everything here is a statement about the broker, not about the adapter's
arithmetic. Consumer groups, pending lists, ``XAUTOCLAIM``, capped trimming and
reconnection have no meaning against a fake: a fake would be asserting that the
code calls the methods it calls, which is the assertion that never fails and
never catches anything.

The unit suite covers the decidable half -- reply shapes, error mapping, budget
arithmetic, refusals -- and deliberately does not duplicate any of this.

Each test gets its own key prefix from the ``namespace`` fixture rather than a
``FLUSHALL``, because ``DHRUVA_TEST_REDIS_URL`` may well point at a Redis
somebody else is using.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.infrastructure.messaging import (
    ENVELOPE_FIELD,
    RedisAck,
    RedisEventStream,
    RedisStreamPublisher,
    stream_key,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.messaging import EventEnvelope, UnsupportedEventVersionError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from redis.asyncio import Redis

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

NOW = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)

#: The idle threshold these tests reclaim at, and the pause that makes an entry
#: genuinely exceed it.
#:
#: Redis measures idle time on its own clock and there is no injecting one, so
#: this is the only place in the suite that waits. It waits fifty times the
#: threshold: a one-millisecond margin is a race a loaded machine loses, and this
#: test did lose it -- passing in one run and failing in the next with nothing
#: changed between them. A margin that large is not a slow test, it is the
#: difference between an assertion and a coin.
RECLAIM_AFTER = timedelta(milliseconds=1)
IDLE_PAUSE_SECONDS = 0.05


def envelope(
    *, sequence: int = 1, aggregate_type: str = "Order", version: int = 1
) -> EventEnvelope:
    """Build a valid envelope. The transport is the subject, not the payload."""
    identifier = uuid4()
    return EventEnvelope(
        event_id=identifier,
        event_type="OrderFilled",
        event_version=version,
        aggregate_type=aggregate_type,
        aggregate_id=UUID(int=sequence),
        sequence=sequence,
        occurred_at=NOW,
        recorded_at=NOW,
        account_id=None,
        correlation_id=identifier,
        causation_id=None,
        payload={"quantity": 5, "symbol": "NIFTY"},
    )


def publisher(client: Redis, namespace: str, *, maxlen: int | None = 1000) -> RedisStreamPublisher:
    """Build a publisher scoped to this test's namespace."""
    return RedisStreamPublisher(client, namespace=namespace, maxlen=maxlen)


def consumer(  # noqa: PLR0913 - each names a distinct axis these tests vary
    client: Redis,
    namespace: str,
    *,
    group: str = "risk",
    name: str = "worker-1",
    aggregate_types: Sequence[str] = ("Order",),
    supported_versions: frozenset[int] | None = None,
) -> RedisEventStream:
    """Build a stream scoped to this test's namespace."""
    return RedisEventStream(
        client,
        aggregate_types=aggregate_types,
        group=group,
        consumer=name,
        namespace=namespace,
        supported_versions=supported_versions,
    )


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


async def test_an_envelope_survives_the_transport_byte_for_byte(
    redis_client: Redis, namespace: str
) -> None:
    """The canonical form is what crosses the wire, unchanged (ADR-061).

    Compared as JSON rather than as objects: equality on the dataclass would pass
    even if a codec had quietly widened an integer or dropped a timezone, because
    both sides would have been decoded by the same code.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    sent = envelope(sequence=42)

    result = await publisher(redis_client, namespace).publish([sent])
    [delivery] = await stream.read(limit=10)

    assert list(result.accepted) == [sent]
    assert delivery.envelope.to_json() == sent.to_json()
    assert delivery.envelope.payload == {"quantity": 5, "symbol": "NIFTY"}


async def test_each_aggregate_type_gets_its_own_stream(redis_client: Redis, namespace: str) -> None:
    """A consumer subscribes to what concerns it rather than filtering a firehose."""
    await publisher(redis_client, namespace).publish(
        [envelope(sequence=1), envelope(sequence=2, aggregate_type="Position")]
    )
    orders = consumer(redis_client, namespace, aggregate_types=("Order",))
    await orders.ensure_group()

    deliveries = await orders.read(limit=10)

    assert [d.envelope.aggregate_type for d in deliveries] == ["Order"]
    assert await redis_client.xlen(stream_key("Position", namespace=namespace)) == 1


async def test_a_group_created_after_the_fact_still_sees_earlier_events(
    redis_client: Redis, namespace: str
) -> None:
    """The group starts at zero, so a consumer may legitimately start after a producer.

    Starting at ``$`` would skip the buffer silently, which is indistinguishable
    from losing it. Redelivery is what the ledger absorbs (ADR-065).
    """
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])
    late = consumer(redis_client, namespace, group="late")
    await late.ensure_group()

    assert len(await late.read(limit=10)) == 1


async def test_a_consumer_may_start_before_any_producer(
    redis_client: Redis, namespace: str
) -> None:
    """``MKSTREAM`` creates the stream, so start order is not a deployment constraint."""
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()

    assert await stream.read(limit=10) == []

    await publisher(redis_client, namespace).publish([envelope(sequence=1)])
    assert len(await stream.read(limit=10)) == 1


async def test_creating_the_group_twice_is_quiet(redis_client: Redis, namespace: str) -> None:
    """``ensure_group`` runs on every process start; the second start must be silent."""
    stream = consumer(redis_client, namespace)

    await stream.ensure_group()
    await stream.ensure_group()

    assert await stream.pending() == 0


# --------------------------------------------------------------------------- #
# Consumer groups
# --------------------------------------------------------------------------- #


async def test_two_consumers_in_one_group_divide_the_work(
    redis_client: Redis, namespace: str
) -> None:
    """The defining property of a group: each entry goes to exactly one member.

    Asserted as a partition rather than as "each got some", because a broker that
    delivered everything to both would still satisfy the weaker statement while
    doubling every side effect downstream.
    """
    first = consumer(redis_client, namespace, name="worker-1")
    second = consumer(redis_client, namespace, name="worker-2")
    await first.ensure_group()
    sent = [envelope(sequence=n) for n in range(1, 7)]
    await publisher(redis_client, namespace).publish(sent)

    taken = [d.envelope.event_id for d in await first.read(limit=3)]
    also = [d.envelope.event_id for d in await second.read(limit=10)]

    assert len(taken) == 3
    assert set(taken).isdisjoint(also)
    assert sorted(taken + also) == sorted(e.event_id for e in sent)


async def test_separate_groups_each_receive_everything(redis_client: Redis, namespace: str) -> None:
    """Fan-out. Risk and reporting are different consumers of the same event."""
    risk = consumer(redis_client, namespace, group="risk")
    reporting = consumer(redis_client, namespace, group="reporting")
    await risk.ensure_group()
    await reporting.ensure_group()
    sent = envelope(sequence=1)

    await publisher(redis_client, namespace).publish([sent])

    assert [d.envelope.event_id for d in await risk.read(limit=10)] == [sent.event_id]
    assert [d.envelope.event_id for d in await reporting.read(limit=10)] == [sent.event_id]


# --------------------------------------------------------------------------- #
# Acknowledgement and the pending list
# --------------------------------------------------------------------------- #


async def test_an_unacknowledged_entry_stays_pending_and_is_not_re_offered(
    redis_client: Redis, namespace: str
) -> None:
    """Read is not delivery. Until the consumer's transaction commits, the entry is held.

    This is what makes ADR-065 possible: acknowledgement happens *after* the
    ledger write, and the window between the two is exactly this pending entry.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])

    assert len(await stream.read(limit=10)) == 1
    assert await stream.pending() == 1
    assert await stream.read(limit=10) == [], "a held entry must not be re-offered"


async def test_acknowledgement_clears_the_pending_entry(
    redis_client: Redis, namespace: str
) -> None:
    """The consumer committed; the broker may forget."""
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])
    [delivery] = await stream.read(limit=10)

    assert await stream.acknowledge([delivery.ack_token]) == 1
    assert await stream.pending() == 0


async def test_acknowledging_the_same_entry_twice_reports_nothing_the_second_time(
    redis_client: Redis, namespace: str
) -> None:
    """A retried acknowledgement is harmless, and says so honestly.

    At-least-once means a consumer may acknowledge twice after a crash between
    its commit and its ack. The count must reflect what actually changed, or a
    caller cannot tell a redelivery from a bug.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])
    [delivery] = await stream.read(limit=10)

    assert await stream.acknowledge([delivery.ack_token]) == 1
    assert await stream.acknowledge([delivery.ack_token]) == 0


async def test_pending_sums_across_every_subscribed_stream(
    redis_client: Redis, namespace: str
) -> None:
    """Consumer lag is a property of the consumer, not of one of its streams (ADR-035)."""
    stream = consumer(redis_client, namespace, aggregate_types=("Order", "Position"))
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish(
        [
            envelope(sequence=1),
            envelope(sequence=2),
            envelope(sequence=3, aggregate_type="Position"),
        ]
    )

    await stream.read(limit=10)

    assert await stream.pending() == 3


async def test_a_token_from_another_namespace_is_refused(
    redis_client: Redis, namespace: str
) -> None:
    """Acking a stream this consumer does not read is a wiring mistake, not a no-op."""
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()

    with pytest.raises(ValidationError):
        await stream.acknowledge([RedisAck(stream="somewhere:else", entry_id="1-0")])


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #


async def test_a_dead_consumers_entries_can_be_reclaimed(
    redis_client: Redis, namespace: str
) -> None:
    """The entries a process was holding when it died are recoverable, not lost.

    Reclaim is explicit rather than folded into ``read`` (see the adapter's
    docstring): it is a supervisor's decision about a process that has stopped,
    and an automatic reclaim inside ``read`` would push a leasing concept into
    replay, where nothing can die.
    """
    dead = consumer(redis_client, namespace, name="worker-dead")
    await dead.ensure_group()
    sent = envelope(sequence=1)
    await publisher(redis_client, namespace).publish([sent])
    [held] = await dead.read(limit=10)

    await asyncio.sleep(IDLE_PAUSE_SECONDS)
    survivor = consumer(redis_client, namespace, name="worker-alive")
    reclaimed = await survivor.reclaim(min_idle=RECLAIM_AFTER, limit=10)

    assert [d.envelope.event_id for d in reclaimed] == [sent.event_id]
    assert held.envelope.event_id == sent.event_id
    assert await survivor.acknowledge([d.ack_token for d in reclaimed]) == 1
    assert await survivor.pending() == 0


async def test_a_live_consumers_entries_are_not_taken_from_it(
    redis_client: Redis, namespace: str
) -> None:
    """The idle threshold is what separates a slow consumer from a dead one.

    Without it, two consumers would take turns stealing the same entry from each
    other and the work would never finish.
    """
    working = consumer(redis_client, namespace, name="worker-busy")
    await working.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])
    await working.read(limit=10)

    other = consumer(redis_client, namespace, name="worker-other")
    reclaimed = await other.reclaim(min_idle=timedelta(minutes=5), limit=10)

    assert reclaimed == []
    assert await working.pending() == 1


async def test_the_client_reconnects_without_the_adapter_noticing(
    redis_client: Redis, namespace: str
) -> None:
    """A dropped connection is not a lost event.

    Redis restarts, networks blip, and a connection pool is expected to recover.
    The adapter must contain no reconnection logic of its own, and this test
    fails if it ever grows some that gets it wrong.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1)])

    await redis_client.connection_pool.disconnect()

    assert len(await stream.read(limit=10)) == 1


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


async def test_a_malformed_entry_raises_and_stays_recoverable(
    redis_client: Redis, namespace: str
) -> None:
    """ADR-022: ambiguity blocks rather than proceeds.

    The entry was delivered under a consumer group, so refusing it does not
    discard it -- it stays in the pending list, where a human can find it and
    ``XAUTOCLAIM`` can retrieve it. Skipping would have processed the events
    around the hole and left nothing showing there was one.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await redis_client.xadd(stream_key("Order", namespace=namespace), {ENVELOPE_FIELD: "{not json"})

    with pytest.raises(ValidationError):
        await stream.read(limit=10)

    assert await stream.pending() == 1


async def test_an_entry_written_by_something_else_is_refused(
    redis_client: Redis, namespace: str
) -> None:
    """Guessing at the meaning of a foreign entry is worse than refusing it."""
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await redis_client.xadd(stream_key("Order", namespace=namespace), {"some-other-field": "1"})

    with pytest.raises(ValidationError):
        await stream.read(limit=10)


async def test_an_envelope_version_this_consumer_cannot_read_is_refused(
    redis_client: Redis, namespace: str
) -> None:
    """A version this consumer cannot read stops it rather than being guessed at.

    ADR-061. Parsing on the assumption that the payload is close enough is how a
    schema change becomes a silent mis-read instead of a loud stop.
    """
    stream = consumer(redis_client, namespace, supported_versions=frozenset({1}))
    await stream.ensure_group()
    await publisher(redis_client, namespace).publish([envelope(sequence=1, version=2)])

    with pytest.raises(UnsupportedEventVersionError):
        await stream.read(limit=10)


async def test_an_unroutable_aggregate_type_never_reaches_redis(
    redis_client: Redis, namespace: str
) -> None:
    """Rejected as terminal, so the relay dead-letters it instead of retrying twelve times."""
    result = await publisher(redis_client, namespace).publish(
        [envelope(sequence=1, aggregate_type="Order:Filled")]
    )

    assert not result.accepted
    [(_, error)] = result.rejected
    assert isinstance(error, ValidationError)
    assert await redis_client.keys(f"{namespace}*") == []


# --------------------------------------------------------------------------- #
# At-least-once, honestly
# --------------------------------------------------------------------------- #


async def test_publishing_the_same_envelope_twice_produces_two_entries(
    redis_client: Redis, namespace: str
) -> None:
    """The transport does not de-duplicate, and this test exists to say so.

    The S05 design proposed explicit stream ids derived from ``sequence`` to make
    a relay retry idempotent here. Redis requires an explicit id to exceed the
    top of the stream, and retry is per message while the stream is per aggregate
    *type* -- so a retried sequence arrives behind later ones and is refused. The
    adapter's docstring records why the scheme was dropped.

    De-duplication therefore lives entirely in the consumer's ledger (ADR-065),
    in the same transaction as the side effect it guards, which is where it could
    protect anything in the first place. A duplicate reaching a consumer is
    correct behaviour under ADR-062, and this asserts it rather than hiding it.
    """
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    twice = envelope(sequence=1)
    outgoing = publisher(redis_client, namespace)

    await outgoing.publish([twice])
    await outgoing.publish([twice])

    deliveries = await stream.read(limit=10)
    assert [d.envelope.event_id for d in deliveries] == [twice.event_id, twice.event_id]
    assert deliveries[0].ack_token != deliveries[1].ack_token


async def test_the_stream_is_trimmed_to_the_configured_length(
    redis_client: Redis, namespace: str
) -> None:
    """The stream is a transport buffer, not storage. The outbox is the record (ADR-062).

    Exact trimming here because the assertion has to mean something; production
    uses approximate trimming, which is what makes it cheap.
    """
    capped = RedisStreamPublisher(redis_client, namespace=namespace, maxlen=5, approximate=False)

    for number in range(1, 21):
        await capped.publish([envelope(sequence=number)])

    assert await redis_client.xlen(stream_key("Order", namespace=namespace)) == 5


async def test_trimming_never_discards_an_entry_a_consumer_is_holding(
    redis_client: Redis, namespace: str
) -> None:
    """A trimmed entry that is still pending is reported as a tombstone, not delivered.

    Redis keeps the pending-list reference after the entry itself is gone, and
    ``XAUTOCLAIM`` reports the pair as nulls. The adapter drops those rather than
    delivering an empty envelope; the row is still in the outbox and unpublished
    events are still republished, so nothing is lost.
    """
    capped = RedisStreamPublisher(redis_client, namespace=namespace, maxlen=1, approximate=False)
    stream = consumer(redis_client, namespace)
    await stream.ensure_group()
    await capped.publish([envelope(sequence=1)])
    await stream.read(limit=10)

    await capped.publish([envelope(sequence=2)])

    await asyncio.sleep(IDLE_PAUSE_SECONDS)
    reclaimed = await stream.reclaim(min_idle=RECLAIM_AFTER, limit=10)
    assert all(d.envelope.sequence == 2 for d in reclaimed)
