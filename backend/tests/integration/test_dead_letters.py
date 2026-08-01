"""Reading and re-queueing dead letters against a real PostgreSQL (ADR-064).

A dead letter is produced the way one is produced in production -- by driving
the relay against a publisher that refuses -- rather than by writing the row
directly. A test that stamped `dead_lettered_at` itself would pass even if the
relay had stopped setting it, which is the failure most worth catching.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.domain.messaging import RequeueRefusal
from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.messaging import (
    DeadLetterStore,
    InMemoryPublisher,
    OutboxRelay,
)
from dhruva.shared.errors import NotFoundError, ValidationError
from dhruva.shared.events import DomainEvent
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

OCCURRED = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)
RECORDED = datetime(2026, 7, 31, 9, 20, tzinfo=UTC)


class OrderRejected(DomainEvent):
    """A minimal event. The dead-letter machinery is the subject."""


async def stage(engine: AsyncEngine, *, count: int = 1) -> list[UUID]:
    """Commit `count` events through the Unit of Work, returning their ids."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    ids: list[UUID] = []
    for index in range(count):
        event = OrderRejected(occurred_at=OCCURRED + timedelta(seconds=index))
        async with SqlAlchemyUnitOfWork(factory, clock=FrozenClock(RECORDED)) as uow:
            uow.add_event(event, aggregate_id=uuid4(), aggregate_type="Order")
            await uow.commit()
        ids.append(event.event_id)
    return ids


async def dead_letter(engine: AsyncEngine, *, count: int = 1) -> list[UUID]:
    """Produce dead letters the way production does: a terminal publish failure."""
    ids = await stage(engine, count=count)
    publisher = InMemoryPublisher(terminal_failures=set(ids))
    outcome = await OutboxRelay(engine, publisher, clock=FrozenClock(RECORDED)).run_once()
    assert outcome.dead_lettered == count, "the fixture must actually dead-letter"
    return ids


async def row(engine: AsyncEngine, event_id: UUID) -> Any:
    """Read one outbox row."""
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT published_at, dead_lettered_at, attempts, last_error, "
                    "next_attempt_at FROM outbox WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        ).one()


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_terminal_failure_appears_in_the_dead_letter_queue(
    migrated: AsyncEngine,
) -> None:
    """The listing carries what an operator decides on, not just identifiers."""
    [event_id] = await dead_letter(migrated)

    [letter] = await DeadLetterStore(migrated).list()

    assert letter.event_id == event_id
    assert letter.event_type == "OrderRejected"
    assert letter.aggregate_type == "Order"
    assert letter.attempts == 1, "a terminal failure spends one attempt, not twelve"
    assert letter.last_error is not None
    assert letter.dead_lettered_at is not None


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_deliverable_event_is_not_in_the_queue(migrated: AsyncEngine) -> None:
    """The predicate is `dead_lettered_at IS NOT NULL` and nothing else."""
    await stage(migrated)

    assert await DeadLetterStore(migrated).count() == 0
    assert await DeadLetterStore(migrated).list() == []


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_queue_is_listed_oldest_first(migrated: AsyncEngine) -> None:
    """A dead-letter queue is read to find the *first* thing that went wrong.

    Newest-first would put the consequences at the top and the cause on the last
    page.
    """
    ids = await dead_letter(migrated, count=4)

    letters = await DeadLetterStore(migrated).list()

    assert [letter.event_id for letter in letters] == ids


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_listing_is_bounded_and_filterable(migrated: AsyncEngine) -> None:
    """An operator looks for a pattern; an unbounded query is one nobody meant to run."""
    await dead_letter(migrated, count=5)
    store = DeadLetterStore(migrated)

    assert len(await store.list(limit=2)) == 2
    assert len(await store.list(event_type="OrderRejected")) == 5
    assert await store.list(event_type="SomethingElse") == []
    assert await store.count() == 5


async def test_a_non_positive_limit_is_refused(migrated: AsyncEngine) -> None:
    """A zero-row listing would look exactly like an empty queue."""
    with pytest.raises(ValidationError):
        await DeadLetterStore(migrated).list(limit=0)


# --------------------------------------------------------------------------- #
# Re-queueing
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_requeueing_returns_the_event_to_the_queue_with_a_fresh_budget(
    migrated: AsyncEngine,
) -> None:
    """The operator has decided the cause is fixed.

    A row returning with its attempts already spent would dead-letter again on
    the first hiccup, which looks exactly like the fix not working.
    """
    [event_id] = await dead_letter(migrated)

    verdict = await DeadLetterStore(migrated).requeue(event_id)

    assert verdict.permitted
    after = await row(migrated, event_id)
    assert after.dead_lettered_at is None
    assert after.attempts == 0
    assert after.last_error is None
    assert after.next_attempt_at is None


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_requeued_event_is_actually_delivered_on_the_next_pass(
    migrated: AsyncEngine,
) -> None:
    """The point of the whole exercise, asserted rather than assumed.

    A re-queue that cleared the flags but left the row invisible to the relay's
    claim query would look successful in every other assertion here.
    """
    [event_id] = await dead_letter(migrated)
    await DeadLetterStore(migrated).requeue(event_id)

    publisher = InMemoryPublisher()
    outcome = await OutboxRelay(migrated, publisher, clock=FrozenClock(RECORDED)).run_once()

    assert outcome.published == 1
    assert publisher.delivery_count(event_id) == 1
    assert (await row(migrated, event_id)).published_at is not None


@pytest.mark.usefixtures("truncated_after_test")
async def test_requeueing_a_published_event_is_refused(migrated: AsyncEngine) -> None:
    """Re-queueing it would manufacture a duplicate on purpose."""
    [event_id] = await stage(migrated)
    await OutboxRelay(migrated, InMemoryPublisher(), clock=FrozenClock(RECORDED)).run_once()

    verdict = await DeadLetterStore(migrated).requeue(event_id)

    assert not verdict.permitted
    assert verdict.refusal is RequeueRefusal.ALREADY_PUBLISHED


@pytest.mark.usefixtures("truncated_after_test")
async def test_requeueing_a_live_event_is_refused(migrated: AsyncEngine) -> None:
    """Resetting a still-retrying row's budget is how a poison message becomes eternal."""
    [event_id] = await stage(migrated)

    verdict = await DeadLetterStore(migrated).requeue(event_id)

    assert not verdict.permitted
    assert verdict.refusal is RequeueRefusal.NOT_DEAD_LETTERED
    assert (await row(migrated, event_id)).dead_lettered_at is None


async def test_requeueing_an_unknown_event_says_so(migrated: AsyncEngine) -> None:
    """Silence would let a typo look like a successful recovery."""
    with pytest.raises(NotFoundError):
        await DeadLetterStore(migrated).requeue(uuid4())


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bulk_requeue_is_bounded_and_reports_each_outcome(
    migrated: AsyncEngine,
) -> None:
    """Returning ten thousand poison messages to a queue in one command is an outage."""
    await dead_letter(migrated, count=5)

    report = await DeadLetterStore(migrated).requeue_all(limit=3)

    assert report.requeued == 3
    assert not report.refused
    assert await DeadLetterStore(migrated).count() == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bulk_requeue_reports_refusals_rather_than_failing(
    migrated: AsyncEngine,
) -> None:
    """A mixed batch must not abort partway and leave the operator guessing."""
    await dead_letter(migrated, count=2)
    published = await stage(migrated)
    await OutboxRelay(migrated, InMemoryPublisher(), clock=FrozenClock(RECORDED)).run_once()
    async with migrated.begin() as connection:
        await connection.execute(
            text("UPDATE outbox SET dead_lettered_at = :now WHERE event_id = :id"),
            {"now": RECORDED, "id": published[0]},
        )

    report = await DeadLetterStore(migrated).requeue_all(limit=10)

    assert report.requeued == 2
    assert report.refused == {RequeueRefusal.ALREADY_PUBLISHED: 1}
