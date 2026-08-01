"""The idempotency ledger against a real PostgreSQL (ADR-065).

The claim under test is not "duplicate inserts are refused" -- any database does
that. It is that **the side effect rolls back with the refusal**, which is a
statement about transactions and cannot be demonstrated without one. A fake
session has no durability boundary to roll back across, so a test against one
would be asserting that the code calls the methods it calls.

Every test that commits uses ``truncated_after_test``: the ledger is exactly the
kind of table whose leftover rows make the next test fail for a reason unrelated
to its subject.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.infrastructure.persistence.ledger import (
    LIVE_RUN_ID,
    DuplicateEventError,
    IdempotencyLedger,
)
from dhruva.shared.errors import ConfigurationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

PROCESSED = datetime(2026, 7, 31, 9, 20, tzinfo=UTC)
GROUP = "risk"

#: The worked-example table S04 established. Used here as *the side effect*: a
#: real row, written in the same transaction as the ledger row, so that "the
#: work rolls back with the duplicate" is a claim about data rather than about
#: control flow.
SIDE_EFFECT = text(
    "INSERT INTO daily_snapshot "
    "(id, account_id, instrument_id, trading_day, close_scaled_units, "
    " turnover_minor_units, currency, version) "
    "VALUES (:row_id, :account, :instrument, :day, 1000000000, 5000, 'INR', 1)"
)


def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory whose commits genuinely commit."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def snapshot_count(engine: AsyncEngine) -> int:
    """Count the side-effect rows currently visible to a fresh connection."""
    async with engine.connect() as connection:
        return int(await connection.scalar(text("SELECT count(*) FROM daily_snapshot")) or 0)


async def ledger_rows(engine: AsyncEngine) -> list[Any]:
    """Return every ledger row, for assertions about what was recorded."""
    async with engine.connect() as connection:
        return list(
            (
                await connection.execute(
                    text(
                        "SELECT consumer_group, event_id, run_id, processed_at "
                        "FROM processed_event ORDER BY processed_at, consumer_group"
                    )
                )
            ).all()
        )


async def process(
    engine: AsyncEngine,
    event_id: UUID,
    *,
    group: str = GROUP,
    run_id: UUID = LIVE_RUN_ID,
    instrument: UUID | None = None,
) -> None:
    """Do the work and claim the event, in one transaction, as a consumer would.

    The order is deliberate and mirrors a real consumer: the side effect first,
    the ledger row second. Claiming first would make the ledger a lock rather
    than a record, and a crash between the two would then suppress work that
    never happened.
    """
    async with sessions(engine)() as session:
        await session.execute(
            SIDE_EFFECT,
            {
                "row_id": uuid4(),
                "account": uuid4(),
                "instrument": instrument or uuid4(),
                "day": PROCESSED.date(),
            },
        )
        ledger = IdempotencyLedger(session, consumer_group=group, run_id=run_id)
        await ledger.record(event_id, processed_at=PROCESSED)
        await session.commit()


# --------------------------------------------------------------------------- #
# The guarantee
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_first_delivery_is_processed_and_recorded(migrated: AsyncEngine) -> None:
    """The happy path, so the refusals below mean something."""
    event_id = uuid4()

    await process(migrated, event_id)

    assert await snapshot_count(migrated) == 1
    [row] = await ledger_rows(migrated)
    assert row.consumer_group == GROUP
    assert row.event_id == event_id
    assert row.run_id == LIVE_RUN_ID
    assert row.processed_at == PROCESSED


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_duplicate_delivery_rolls_back_the_side_effect_with_it(
    migrated: AsyncEngine,
) -> None:
    """The whole of ADR-065 in one assertion.

    At-least-once (ADR-062) guarantees this happens. What must not happen is a
    second row in ``daily_snapshot`` -- and the only reason it does not is that
    the ledger write shares a transaction with it. A ledger in its own
    transaction would have refused the duplicate *and* left the second side
    effect committed, which is the failure this design exists to prevent.
    """
    event_id = uuid4()
    await process(migrated, event_id)

    with pytest.raises(DuplicateEventError):
        await process(migrated, event_id)

    assert await snapshot_count(migrated) == 1, "the redelivered work must not have happened twice"
    assert len(await ledger_rows(migrated)) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_two_consumer_groups_each_process_the_same_event(
    migrated: AsyncEngine,
) -> None:
    """Risk and reporting are separate consumers of one event, and both must run.

    Keyed on ``event_id`` alone, whichever group arrived first would silently
    suppress the other -- and the suppressed one would report nothing wrong.
    """
    event_id = uuid4()

    await process(migrated, event_id, group="risk")
    await process(migrated, event_id, group="reporting")

    assert await snapshot_count(migrated) == 2
    assert {row.consumer_group for row in await ledger_rows(migrated)} == {"risk", "reporting"}


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_replay_run_processes_history_the_live_run_already_processed(
    migrated: AsyncEngine,
) -> None:
    """Why ``run_id`` is in the key at all.

    Keyed by ``(consumer_group, event_id)``, a backtest replaying the same
    history would find every row present and process nothing -- reporting zero
    trades. A zero that looks like a result is worse than an error, because
    nobody investigates it.
    """
    event_id = uuid4()
    await process(migrated, event_id)

    await process(migrated, event_id, run_id=uuid4())

    assert await snapshot_count(migrated) == 2
    assert len({row.run_id for row in await ledger_rows(migrated)}) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_replay_repeated_twice_processes_history_twice(
    migrated: AsyncEngine,
) -> None:
    """Each replay allocates a fresh run, so re-running a backtest re-runs it."""
    event_id = uuid4()

    await process(migrated, event_id, run_id=uuid4())
    await process(migrated, event_id, run_id=uuid4())

    assert await snapshot_count(migrated) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_duplicate_inside_one_run_is_still_refused(migrated: AsyncEngine) -> None:
    """Run scoping widens the key; it does not switch deduplication off."""
    event_id = uuid4()
    run = uuid4()
    await process(migrated, event_id, run_id=run)

    with pytest.raises(DuplicateEventError):
        await process(migrated, event_id, run_id=run)

    assert await snapshot_count(migrated) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_conflict_surfaces_on_the_claim_rather_than_at_commit(
    migrated: AsyncEngine,
) -> None:
    """``record`` flushes, so the caller learns *which* event was the duplicate.

    Deferring to commit would report a violation for a batch with no indication
    of which event caused it, and a consumer would have nothing to acknowledge
    and nothing to log.
    """
    event_id = uuid4()
    await process(migrated, event_id)

    async with sessions(migrated)() as session:
        ledger = IdempotencyLedger(session, consumer_group=GROUP)
        with pytest.raises(DuplicateEventError) as caught:
            await ledger.record(event_id, processed_at=PROCESSED)
        await session.rollback()

    assert caught.value.context["event_id"] == str(event_id)
    assert caught.value.context["consumer_group"] == GROUP


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_pruning_removes_only_rows_older_than_the_cutoff(
    migrated: AsyncEngine,
) -> None:
    """Retention must not delete a row the transport could still redeliver against."""
    old, recent = uuid4(), uuid4()
    async with sessions(migrated)() as session:
        ledger = IdempotencyLedger(session, consumer_group=GROUP)
        await ledger.record(old, processed_at=PROCESSED - timedelta(days=40))
        await ledger.record(recent, processed_at=PROCESSED)
        await session.commit()

    async with sessions(migrated)() as session:
        removed = await IdempotencyLedger(session, consumer_group=GROUP).prune_before(
            PROCESSED - timedelta(days=30)
        )
        await session.commit()

    assert removed == 1
    assert [row.event_id for row in await ledger_rows(migrated)] == [recent]


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_replay_prunes_its_own_run_and_leaves_live_history_alone(
    migrated: AsyncEngine,
) -> None:
    """ADR-065: a replay cleans up after itself, and only after itself."""
    run = uuid4()
    async with sessions(migrated)() as session:
        await IdempotencyLedger(session, consumer_group=GROUP).record(
            uuid4(), processed_at=PROCESSED
        )
        await IdempotencyLedger(session, consumer_group=GROUP, run_id=run).record(
            uuid4(), processed_at=PROCESSED
        )
        await session.commit()

    async with sessions(migrated)() as session:
        removed = await IdempotencyLedger(session, consumer_group=GROUP, run_id=run).prune_run()
        await session.commit()

    assert removed == 1
    assert [row.run_id for row in await ledger_rows(migrated)] == [LIVE_RUN_ID]


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


async def test_a_ledger_without_a_consumer_group_refuses_to_be_built() -> None:
    """An unnamed group would share one ledger with every other consumer."""
    with pytest.raises(ConfigurationError):
        IdempotencyLedger(None, consumer_group="")  # type: ignore[arg-type]


async def test_the_live_run_identifier_cannot_be_generated_by_accident() -> None:
    """``uuid4`` will not produce the nil UUID, so live and replay cannot collide."""
    assert UUID(int=0) == LIVE_RUN_ID
    assert uuid4() != LIVE_RUN_ID
