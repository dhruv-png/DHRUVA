"""Relay crash semantics against a real PostgreSQL (ADR-062, ADR-063, ADR-064).

Every guarantee here is a statement about what survives a process dying at a
specific instant. A fake session cannot demonstrate that: there is no durability
boundary to crash between, so a test against one would be asserting that the code
calls the methods it calls.

The relay is deliberately driven one pass at a time rather than run as a loop.
A loop would make these tests depend on timing, and a test that waits is a test
that is flaky on a loaded machine and silent about why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.domain.messaging import RetryPolicy
from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.messaging import InMemoryPublisher, OutboxRelay
from dhruva.shared.events import DomainEvent
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

OCCURRED = datetime(2026, 7, 28, 9, 15, tzinfo=UTC)
RECORDED = datetime(2026, 7, 28, 9, 20, tzinfo=UTC)


class OrderFilled(DomainEvent):
    """A minimal event. The relay is the subject, not the payload."""


async def _stage(
    engine: AsyncEngine, *, count: int = 1, aggregate: UUID | None = None
) -> list[UUID]:
    """Commit `count` events to the outbox, returning their ids in order."""
    aggregate_id = aggregate or uuid4()
    ids: list[UUID] = []
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    for index in range(count):
        event = OrderFilled(occurred_at=OCCURRED + timedelta(seconds=index))
        async with SqlAlchemyUnitOfWork(factory, clock=FrozenClock(RECORDED)) as uow:
            uow.add_event(event, aggregate_id=aggregate_id, aggregate_type="Order")
            await uow.commit()
        ids.append(event.event_id)
    return ids


def _relay(  # noqa: PLR0913 - each argument configures a distinct relay behaviour
    engine: AsyncEngine,
    publisher: InMemoryPublisher,
    *,
    now: datetime = RECORDED,
    lease: timedelta = timedelta(seconds=30),
    policy: RetryPolicy | None = None,
    identity: UUID | None = None,
) -> OutboxRelay:
    return OutboxRelay(
        engine,
        publisher,
        clock=FrozenClock(now),
        lease=lease,
        policy=policy,
        identity=identity,
    )


async def _row(engine: AsyncEngine, event_id: UUID) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT published_at, attempts, next_attempt_at, claimed_at, "
                    "claimed_by, dead_lettered_at, last_error, aggregate_type "
                    "FROM outbox WHERE event_id = :id"
                ),
                {"id": event_id},
            )
        ).one()


async def _unpublished(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM outbox "
                    "WHERE published_at IS NULL AND dead_lettered_at IS NULL"
                )
            )
        ) or 0


# --------------------------------------------------------------------------- #
# The happy path, so the failures below mean something
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_staged_event_is_published_and_marked(migrated: AsyncEngine) -> None:
    """One pass moves a row from unpublished to published, exactly once."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher()

    outcome = await _relay(migrated, publisher).run_once()

    assert outcome.claimed == 1
    assert outcome.published == 1
    assert publisher.delivery_count(event_id) == 1
    row = await _row(migrated, event_id)
    assert row.published_at is not None
    assert row.claimed_at is None, "the lease must be released once published"
    assert row.aggregate_type == "Order", "the persisted routing key must reach the envelope"


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_idle_relay_reports_idle(migrated: AsyncEngine) -> None:
    """Nothing to do is not an error, and the caller needs to know to back off."""
    outcome = await _relay(migrated, InMemoryPublisher()).run_once()

    assert outcome.idle
    assert outcome.claimed == 0


# --------------------------------------------------------------------------- #
# 1. Crash before publish
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_crash_before_publishing_loses_nothing(migrated: AsyncEngine) -> None:
    """The row is claimed but never published; a later relay must still deliver it.

    Simulated by claiming and then abandoning the relay entirely -- which is what
    a SIGKILL between the claim commit and the publish call actually leaves
    behind.
    """
    (event_id,) = await _stage(migrated)
    crashed = _relay(migrated, InMemoryPublisher(), lease=timedelta(seconds=30))

    claimed = await crashed._claim()
    assert len(claimed) == 1

    row = await _row(migrated, event_id)
    assert row.published_at is None, "nothing was published, so nothing may be marked"
    assert row.claimed_at is not None, "the row is leased to the dead relay"

    # A relay arriving after the lease expires picks it up and delivers it.
    survivor = InMemoryPublisher()
    later = _relay(migrated, survivor, now=RECORDED + timedelta(minutes=5))

    outcome = await later.run_once()

    assert outcome.published == 1
    assert survivor.delivery_count(event_id) == 1
    assert await _unpublished(migrated) == 0


# --------------------------------------------------------------------------- #
# 2. Crash after publish, before marking published
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_crash_after_publishing_redelivers_rather_than_losing(
    migrated: AsyncEngine,
) -> None:
    """The defining at-least-once scenario (ADR-062).

    The transport has the event and the database does not know. On restart the
    row is still unpublished, so it is published again. **The duplicate is
    correct.** Marking before publishing would turn this into silent loss, and
    losing an event about capital is strictly worse than repeating one.
    """
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher()
    crashed = _relay(migrated, publisher)

    claimed = await crashed._claim()
    await publisher.publish([envelope for envelope, _ in claimed])
    # Process dies here: published, not marked.

    assert publisher.delivery_count(event_id) == 1
    row = await _row(migrated, event_id)
    assert row.published_at is None

    restarted = _relay(migrated, publisher, now=RECORDED + timedelta(minutes=5))
    outcome = await restarted.run_once()

    assert outcome.published == 1
    assert publisher.delivery_count(event_id) == 2, "at-least-once permits the duplicate"
    assert event_id in publisher.duplicated_ids()
    assert await _unpublished(migrated) == 0


# --------------------------------------------------------------------------- #
# 3. Relay restart
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_restarted_relay_resumes_from_durable_state(migrated: AsyncEngine) -> None:
    """State lives in the table, not in the relay, so a fresh instance continues.

    A new relay has a new identity and no memory of the previous one. It must
    still find the outstanding work, because progress is a column rather than
    anything the process held.
    """
    ids = await _stage(migrated, count=5)
    publisher = InMemoryPublisher()

    first = OutboxRelay(
        migrated, publisher, clock=FrozenClock(RECORDED), batch_size=2, identity=uuid4()
    )
    assert (await first.run_once()).published == 2

    second = OutboxRelay(
        migrated, publisher, clock=FrozenClock(RECORDED), batch_size=10, identity=uuid4()
    )
    outcome = await second.run_once()

    assert outcome.published == 3
    assert publisher.unique_published_ids == set(ids)
    assert await _unpublished(migrated) == 0


# --------------------------------------------------------------------------- #
# 4. Duplicate publication
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_marking_published_is_idempotent(migrated: AsyncEngine) -> None:
    """A second mark must not resurrect or double-count an already-published row.

    `WHERE published_at IS NULL` in the update is what makes the second attempt a
    no-op rather than a silent overwrite of the original delivery time.
    """
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher()
    relay = _relay(migrated, publisher)

    assert (await relay.run_once()).published == 1
    first_row = await _row(migrated, event_id)

    marked_again = await relay._mark_published([event_id])

    assert marked_again == 0, "an already-published row must not be marked twice"
    assert (await _row(migrated, event_id)).published_at == first_row.published_at


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_published_row_is_never_claimed_again(migrated: AsyncEngine) -> None:
    """Delivery happens once per row unless a crash intervenes."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher()
    relay = _relay(migrated, publisher)

    await relay.run_once()
    second = await relay.run_once()

    assert second.idle
    assert publisher.delivery_count(event_id) == 1


# --------------------------------------------------------------------------- #
# 5. Concurrent relays and SKIP LOCKED
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_two_relays_partition_the_work_without_overlap(
    migrated: AsyncEngine,
) -> None:
    """`FOR UPDATE SKIP LOCKED` is what makes horizontal scaling free (ADR-063).

    Two relays run in turn against the same table. Neither may deliver an event
    the other already took, and between them they must deliver everything --
    partition, not duplication and not loss.
    """
    ids = await _stage(migrated, count=6)
    left, right = InMemoryPublisher(), InMemoryPublisher()

    alpha = OutboxRelay(migrated, left, clock=FrozenClock(RECORDED), batch_size=3, identity=uuid4())
    beta = OutboxRelay(migrated, right, clock=FrozenClock(RECORDED), batch_size=3, identity=uuid4())

    first = await alpha.run_once()
    second = await beta.run_once()

    assert first.published == 3
    assert second.published == 3
    assert not (left.unique_published_ids & right.unique_published_ids), "overlap"
    assert left.unique_published_ids | right.unique_published_ids == set(ids)
    assert await _unpublished(migrated) == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_leased_row_is_invisible_to_another_relay(migrated: AsyncEngine) -> None:
    """A live lease reserves the row without holding a database lock."""
    await _stage(migrated, count=1)
    holder = _relay(migrated, InMemoryPublisher(), identity=uuid4())

    await holder._claim()

    other = _relay(migrated, InMemoryPublisher(), identity=uuid4())
    assert (await other.run_once()).idle, "a live lease must exclude another relay"


# --------------------------------------------------------------------------- #
# 6. Lease expiry and reclaim
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_expired_lease_is_reclaimed_by_another_relay(
    migrated: AsyncEngine,
) -> None:
    """The recovery path after a relay dies holding claims.

    Time is advanced by constructing a relay with a later frozen clock rather
    than by sleeping. A test that sleeps for a lease interval is a slow test that
    is also flaky on a loaded machine.
    """
    (event_id,) = await _stage(migrated)
    dead_identity, live_identity = uuid4(), uuid4()

    dead = _relay(
        migrated, InMemoryPublisher(), lease=timedelta(seconds=30), identity=dead_identity
    )
    await dead._claim()

    assert (await _row(migrated, event_id)).claimed_by == dead_identity

    survivor = InMemoryPublisher()
    live = _relay(
        migrated,
        survivor,
        now=RECORDED + timedelta(seconds=31),
        lease=timedelta(seconds=30),
        identity=live_identity,
    )

    outcome = await live.run_once()

    assert outcome.published == 1
    assert survivor.delivery_count(event_id) == 1
    assert (await _row(migrated, event_id)).published_at is not None


# --------------------------------------------------------------------------- #
# 7. Retry, backoff and the dead-letter state
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_transient_failure_schedules_a_retry_via_next_attempt_at(
    migrated: AsyncEngine,
) -> None:
    """A rejected envelope is not lost and not immediately retried (ADR-064)."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher(transient_failures={event_id: 1})

    outcome = await _relay(migrated, publisher).run_once()

    assert outcome.retried == 1
    assert outcome.published == 0
    row = await _row(migrated, event_id)
    assert row.published_at is None
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > RECORDED
    assert row.claimed_at is None, "a failed row must not stay leased"
    assert "transport refused" in (row.last_error or "")


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_row_is_not_retried_before_next_attempt_at(migrated: AsyncEngine) -> None:
    """The backoff is honoured by the claim query, not merely recorded."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher(transient_failures={event_id: 5})

    await _relay(migrated, publisher).run_once()
    immediate = await _relay(migrated, publisher).run_once()

    assert immediate.idle, "a backed-off row must be invisible until its time comes"


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_transient_failure_eventually_succeeds(migrated: AsyncEngine) -> None:
    """Two failures then delivery, with time advanced rather than slept."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher(transient_failures={event_id: 2})
    policy = RetryPolicy(base=timedelta(seconds=1), cap=timedelta(seconds=4), max_attempts=12)

    for minute in range(3):
        await _relay(
            migrated, publisher, now=RECORDED + timedelta(minutes=minute), policy=policy
        ).run_once()

    assert publisher.delivery_count(event_id) == 1
    assert (await _row(migrated, event_id)).published_at is not None


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_terminal_failure_dead_letters_without_consuming_retries(
    migrated: AsyncEngine,
) -> None:
    """A malformed payload will not parse on the fourth attempt either (ADR-064)."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher(terminal_failures={event_id})

    outcome = await _relay(migrated, publisher).run_once()

    assert outcome.dead_lettered == 1
    assert outcome.retried == 0
    row = await _row(migrated, event_id)
    assert row.dead_lettered_at is not None
    assert row.attempts == 1, "a terminal failure must not burn the retry budget"
    assert row.published_at is None


@pytest.mark.usefixtures("truncated_after_test")
async def test_retry_exhaustion_leads_to_the_dead_letter_state(
    migrated: AsyncEngine,
) -> None:
    """After the attempt ceiling the row stops competing with deliverable work."""
    (event_id,) = await _stage(migrated)
    publisher = InMemoryPublisher(transient_failures={event_id: 50})
    policy = RetryPolicy(base=timedelta(seconds=1), cap=timedelta(seconds=1), max_attempts=3)

    for minute in range(4):
        await _relay(
            migrated, publisher, now=RECORDED + timedelta(minutes=minute), policy=policy
        ).run_once()

    row = await _row(migrated, event_id)
    assert row.dead_lettered_at is not None
    assert row.attempts == 3
    assert row.published_at is None


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_dead_lettered_row_is_never_claimed_again(migrated: AsyncEngine) -> None:
    """A poison message must not block the head or consume further passes."""
    (poison,) = await _stage(migrated)
    publisher = InMemoryPublisher(terminal_failures={poison})

    await _relay(migrated, publisher).run_once()
    after = await _relay(migrated, publisher, now=RECORDED + timedelta(hours=1)).run_once()

    assert after.idle


@pytest.mark.usefixtures("truncated_after_test")
async def test_one_poison_message_does_not_block_the_others(migrated: AsyncEngine) -> None:
    """The batch survives a bad message; the rest are delivered in the same pass."""
    ids = await _stage(migrated, count=4)
    poison = ids[1]
    publisher = InMemoryPublisher(terminal_failures={poison})

    outcome = await _relay(migrated, publisher).run_once()

    assert outcome.published == 3
    assert outcome.dead_lettered == 1
    assert publisher.unique_published_ids == set(ids) - {poison}


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_broker_outage_loses_nothing_and_recovers(migrated: AsyncEngine) -> None:
    """The whole transport is down, then returns. Every event must arrive."""
    ids = await _stage(migrated, count=4)
    publisher = InMemoryPublisher(unavailable=True)
    policy = RetryPolicy(base=timedelta(seconds=1), cap=timedelta(seconds=2), max_attempts=12)

    down = await _relay(migrated, publisher, policy=policy).run_once()
    assert down.retried == 4
    assert down.published == 0

    publisher.unavailable = False
    recovered = await _relay(
        migrated, publisher, now=RECORDED + timedelta(minutes=1), policy=policy
    ).run_once()

    assert recovered.published == 4
    assert publisher.unique_published_ids == set(ids)
    assert await _unpublished(migrated) == 0
