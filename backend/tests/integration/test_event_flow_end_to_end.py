"""A domain event from a transaction to a consumer, through everything (S05).

The pieces are each tested on their own: the Unit of Work stages, the relay
drains, the adapter translates, the consumer reads. This module is the only
place that asserts they *compose*, and composition is where the interesting
faults live -- one component's output format quietly disagreeing with the next
one's expectations, which every isolated test passes straight over because each
side is self-consistent.

Real PostgreSQL and real Redis, because the claim being made spans both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.messaging import (
    OutboxRelay,
    RedisEventStream,
    RedisStreamPublisher,
)
from dhruva.shared.events import DomainEvent
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

OCCURRED = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)
RECORDED = datetime(2026, 7, 31, 9, 20, tzinfo=UTC)
AGGREGATE_TYPE = "Order"


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderFilled(DomainEvent):
    """An event carrying values that a lax encoder would quietly flatten.

    Every field here is deliberate. A ``Decimal`` becomes a wrong number if
    anything routes it through a float; a ``datetime`` becomes a string if the
    encoder has no type discriminator; a ``UUID`` likewise. Those are exactly the
    losses ADR-061's codecs exist to prevent, and an event of nothing but
    integers and strings would have proved nothing about whether they are
    actually on the path.
    """

    instrument_id: UUID
    filled_at: datetime
    average_price: Decimal
    lots: int


def an_event(*, lots: int = 5, at: datetime = OCCURRED) -> OrderFilled:
    """Build one filled-order event."""
    return OrderFilled(
        occurred_at=at,
        instrument_id=UUID("11111111-2222-3333-4444-555555555555"),
        filled_at=at + timedelta(milliseconds=250),
        average_price=Decimal("21456.75"),
        lots=lots,
    )


async def stage(
    engine: AsyncEngine,
    *events: OrderFilled,
    aggregate_id: UUID | None = None,
    aggregate_type: str = AGGREGATE_TYPE,
) -> UUID:
    """Commit events through the Unit of Work, as a use case would (AR-001b)."""
    identity = aggregate_id or uuid4()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    for event in events:
        async with SqlAlchemyUnitOfWork(factory, clock=FrozenClock(RECORDED)) as uow:
            uow.add_event(event, aggregate_id=identity, aggregate_type=aggregate_type)
            await uow.commit()
    return identity


def relay(engine: AsyncEngine, publisher: RedisStreamPublisher) -> OutboxRelay:
    """Build a relay with a frozen clock, so nothing here waits."""
    return OutboxRelay(engine, publisher, clock=FrozenClock(RECORDED))


async def consumer(client: Redis, namespace: str) -> RedisEventStream:
    """Build a consumer with its group already created."""
    stream = RedisEventStream(
        client,
        aggregate_types=(AGGREGATE_TYPE,),
        group="e2e",
        consumer="worker-1",
        namespace=namespace,
    )
    await stream.ensure_group()
    return stream


async def outbox_row(engine: AsyncEngine, event_id: UUID) -> Any:
    """Read the durable record of one event."""
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT published_at, dead_lettered_at, attempts, aggregate_type "
                    "FROM outbox WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        ).one()


# --------------------------------------------------------------------------- #
# The whole path
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_event_reaches_a_consumer_with_every_value_intact(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """The claim S05 exists to make: commit a fact, and a consumer receives it.

    The assertions are about *types*, not just values. A payload that arrives as
    ``'21456.75'`` instead of ``Decimal('21456.75')`` is a payload that will be
    added to another number by a consumer that has no reason to suspect it, and
    the resulting position will be wrong in a way nothing reports.
    """
    stream = await consumer(redis_client, namespace)
    event = an_event()
    await stage(migrated, event)

    outcome = await relay(
        migrated, RedisStreamPublisher(redis_client, namespace=namespace)
    ).run_once()

    assert outcome.published == 1
    [delivery] = await stream.read(limit=10)

    assert delivery.envelope.event_id == event.event_id
    assert delivery.envelope.event_type == "OrderFilled"
    assert delivery.envelope.aggregate_type == AGGREGATE_TYPE
    assert delivery.envelope.occurred_at == OCCURRED
    assert delivery.envelope.recorded_at == RECORDED

    payload = delivery.envelope.payload
    assert payload["average_price"] == Decimal("21456.75")
    assert isinstance(payload["average_price"], Decimal)
    assert payload["filled_at"] == event.filled_at
    assert isinstance(payload["filled_at"], datetime)
    assert payload["instrument_id"] == event.instrument_id
    assert isinstance(payload["instrument_id"], UUID)
    assert payload["lots"] == 5


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_two_timestamps_stay_distinct_all_the_way_through(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """ADR-007. Conflating them anywhere on this path is how lookahead bias enters.

    ``occurred_at`` is when the fact became true; ``recorded_at`` is when the
    platform learned it. A backtest that filtered on the wrong one would trade on
    information it could not have had, and would look profitable.
    """
    stream = await consumer(redis_client, namespace)
    await stage(migrated, an_event())

    await relay(migrated, RedisStreamPublisher(redis_client, namespace=namespace)).run_once()
    [delivery] = await stream.read(limit=10)

    assert delivery.envelope.occurred_at == OCCURRED
    assert delivery.envelope.recorded_at == RECORDED
    assert delivery.envelope.occurred_at < delivery.envelope.recorded_at


@pytest.mark.usefixtures("truncated_after_test")
async def test_events_for_one_aggregate_arrive_in_the_order_they_were_committed(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """Per-aggregate ordering, which is the whole of what ADR-062 promises live."""
    stream = await consumer(redis_client, namespace)
    events = [an_event(lots=n, at=OCCURRED + timedelta(seconds=n)) for n in range(1, 6)]
    await stage(migrated, *events)

    await relay(migrated, RedisStreamPublisher(redis_client, namespace=namespace)).run_once()

    deliveries = await stream.read(limit=10)
    assert [d.envelope.payload["lots"] for d in deliveries] == [1, 2, 3, 4, 5]
    assert [d.envelope.event_id for d in deliveries] == [e.event_id for e in events]


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_outbox_row_is_marked_only_after_the_broker_has_it(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """Publish then mark, never the reverse (ADR-062).

    Asserted end to end because the ordering is only meaningful if the thing
    being marked is the thing the broker actually received.
    """
    event = an_event()
    await stage(migrated, event)

    before = await outbox_row(migrated, event.event_id)
    assert before.published_at is None

    await relay(migrated, RedisStreamPublisher(redis_client, namespace=namespace)).run_once()

    after = await outbox_row(migrated, event.event_id)
    assert after.published_at is not None
    assert after.aggregate_type == AGGREGATE_TYPE


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_relay_that_dies_after_publishing_redelivers_rather_than_losing(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """The at-least-once window, end to end (ADR-062).

    The broker has the event and the database does not know. On restart the row
    is still unpublished, so it is published again and the consumer sees it
    twice. **The duplicate is correct**: marking first would turn this into
    silent loss, and losing an event about capital is strictly worse than
    repeating one. De-duplication is the consumer's ledger (ADR-065), in the same
    transaction as the side effect it guards.
    """
    stream = await consumer(redis_client, namespace)
    publisher = RedisStreamPublisher(redis_client, namespace=namespace)
    event = an_event()
    await stage(migrated, event)

    crashed = relay(migrated, publisher)
    claimed = await crashed._claim()
    await publisher.publish([envelope for envelope, _ in claimed])
    # The process dies here: the broker has it, the outbox does not know.

    assert (await outbox_row(migrated, event.event_id)).published_at is None

    restarted = OutboxRelay(migrated, publisher, clock=FrozenClock(RECORDED + timedelta(minutes=5)))
    assert (await restarted.run_once()).published == 1

    deliveries = await stream.read(limit=10)
    assert [d.envelope.event_id for d in deliveries] == [event.event_id, event.event_id]


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_unroutable_event_dead_letters_without_reaching_the_broker(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """A terminal failure spends no retry budget and publishes nothing (ADR-064).

    The aggregate type cannot appear in a Redis key, so the adapter refuses it
    before any command is sent. That refusal is a ``ValidationError``, which the
    delivery policy classifies as terminal -- so the row moves aside on the first
    attempt instead of being retried twelve times while every well-behaved event
    queues behind it.
    """
    event = an_event()
    await stage(migrated, event, aggregate_type="Order:Filled")

    outcome = await relay(
        migrated, RedisStreamPublisher(redis_client, namespace=namespace)
    ).run_once()

    assert outcome.dead_lettered == 1
    assert outcome.published == 0
    row = await outbox_row(migrated, event.event_id)
    assert row.dead_lettered_at is not None
    assert row.published_at is None
    assert await redis_client.keys(f"{namespace}*") == []


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_second_pass_over_a_drained_outbox_publishes_nothing(
    migrated: AsyncEngine, redis_client: Redis, namespace: str
) -> None:
    """Idle is idle. A relay that republished on every pass would flood the bus."""
    publisher = RedisStreamPublisher(redis_client, namespace=namespace)
    await stage(migrated, an_event())
    draining = relay(migrated, publisher)

    assert (await draining.run_once()).published == 1
    second = await draining.run_once()

    assert second.idle
    assert second.published == 0
