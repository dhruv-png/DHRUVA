"""The outbox against a real PostgreSQL (AR-001b, ADR-058).

These are the first tests the outbox has ever had. S04 shipped `OutboxWriter`
with no caller and no coverage, and the canonical validation reported nineteen
passing integration tests while this table was only ever truncated by a fixture,
never asserted on. Presence in test infrastructure read as coverage.

What a fake cannot check, and these do
--------------------------------------
That the row and the aggregate change **share one transaction**. A fake session
records calls; only a real one can be rolled back by the database and show that
nothing survived. The durability boundary is the subject here, so the subject has
to be real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
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


class SomethingHappened(DomainEvent):
    """A minimal event. The subject here is the outbox, not the payload."""


def _uow(engine: AsyncEngine) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        clock=FrozenClock(RECORDED),
    )


async def _count(engine: AsyncEngine, event_id: object) -> int:
    async with engine.connect() as connection:
        return (
            await connection.scalar(
                text("SELECT count(*) FROM outbox WHERE event_id = :id"), {"id": event_id}
            )
        ) or 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_staged_event_survives_the_commit_that_carried_it(
    migrated: AsyncEngine,
) -> None:
    """The row is durable once the transaction commits. This is the guarantee."""
    event = SomethingHappened(occurred_at=OCCURRED)

    async with _uow(migrated) as uow:
        uow.add_event(event)
        await uow.commit()

    assert await _count(migrated, event.event_id) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_rollback_leaves_no_outbox_row(migrated: AsyncEngine) -> None:
    """No consumer may observe an event for work that did not happen.

    Under the previous design this held because a `finally` block cleared a list.
    It now holds because the database rolled the row back, which is a guarantee
    rather than a convention.
    """
    event = SomethingHappened(occurred_at=OCCURRED)

    # PT012: the block needs several statements because the *transaction* is the
    # subject -- the failure has to happen inside an open unit of work, which is
    # not expressible as a single call.
    with pytest.raises(RuntimeError, match="deliberate"):  # noqa: PT012
        async with _uow(migrated) as uow:
            uow.add_event(event)
            raise RuntimeError("deliberate")

    assert await _count(migrated, event.event_id) == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_event_and_the_aggregate_change_share_one_fate(
    migrated: AsyncEngine,
) -> None:
    """The point of an outbox, asserted directly.

    A row is written alongside the event and the transaction is then failed. If
    the two did not share a transaction, one of them would survive -- which is
    precisely the dual-write bug AR-001b removed.
    """
    event = SomethingHappened(occurred_at=OCCURRED)
    snapshot_id = uuid4()

    with pytest.raises(RuntimeError, match="deliberate"):  # noqa: PT012 - see above
        async with _uow(migrated) as uow:
            await uow.session.execute(
                text(
                    "INSERT INTO daily_snapshot (id, account_id, instrument_id, "
                    "trading_day, close_scaled_units, turnover_minor_units, "
                    "currency, version) VALUES (:id, :a, :i, :d, 1, 1, 'INR', 1)"
                ),
                {"id": snapshot_id, "a": uuid4(), "i": uuid4(), "d": OCCURRED.date()},
            )
            uow.add_event(event)
            raise RuntimeError("deliberate")

    assert await _count(migrated, event.event_id) == 0
    async with migrated.connect() as connection:
        snapshots = await connection.scalar(
            text("SELECT count(*) FROM daily_snapshot WHERE id = :id"), {"id": snapshot_id}
        )
    assert snapshots == 0, "the aggregate change survived a transaction the event did not"


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_row_carries_the_bitemporal_pair_and_starts_unpublished(
    migrated: AsyncEngine,
) -> None:
    """`recorded_at` from the injected clock, `occurred_at` from the event.

    A relay finds work by `published_at IS NULL` (ADR-062), so a freshly staged
    row must start there -- and the two timestamps must differ, because a replay
    bounded by `as_of` (ADR-069) is only meaningful if they do.
    """
    event = SomethingHappened(occurred_at=OCCURRED)

    async with _uow(migrated) as uow:
        uow.add_event(event)
        await uow.commit()

    async with migrated.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT occurred_at, recorded_at, published_at, attempts, "
                    "event_type, event_version FROM outbox WHERE event_id = :id"
                ),
                {"id": event.event_id},
            )
        ).one()

    assert row.occurred_at == OCCURRED
    assert row.recorded_at == RECORDED
    assert row.occurred_at != row.recorded_at
    assert row.published_at is None, "a new row must be visible to the relay"
    assert row.attempts == 0
    assert row.event_type == "SomethingHappened"
    assert row.event_version == 1, "migration 0003 backfills version 1"


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_event_id_is_unique_across_the_outbox(migrated: AsyncEngine) -> None:
    """Consumers deduplicate on `event_id` (ADR-065), so the column must enforce it.

    Staging the same event twice is a producer bug. The database refuses it rather
    than leaving two rows a consumer would have to reconcile.
    """
    event = SomethingHappened(occurred_at=OCCURRED)

    async with _uow(migrated) as uow:
        uow.add_event(event)
        await uow.commit()

    with pytest.raises(Exception, match="event_id"):  # noqa: PT012 - see above
        async with _uow(migrated) as second:
            second.add_event(event)
            await second.commit()
