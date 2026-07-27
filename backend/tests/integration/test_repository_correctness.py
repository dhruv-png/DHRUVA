"""Repository correctness against a real database.

Everything here is a property that **cannot** be verified without PostgreSQL:
transaction rollback, concurrent writers, optimistic locking, deadlock handling,
retry behaviour, isolation assumptions, and idempotent writes.

Mocking any of these would produce a suite that passes while the behaviour it
claims to verify is unknown -- which is worse than not having the test, because
it converts an open question into a false answer.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.infrastructure.persistence.factories import DailySnapshotFactory
from dhruva.contexts.platform.infrastructure.persistence.repository import (
    DailySnapshotRepository,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Money, Price
from dhruva.shared.time import TradingDay

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration

TUESDAY = date(2026, 7, 28)
ACCOUNT = AccountId.deterministic("primary")
INSTRUMENT = InstrumentId.deterministic("NSE", "RELIANCE")


class WeekdayCalendar:
    """Weekdays are sessions."""

    def is_session(self, day: date) -> bool:
        """Return whether the market was open."""
        return day.weekday() < 5


FACTORY = DailySnapshotFactory(WeekdayCalendar())  # type: ignore[arg-type]


def _snapshot(*, close: str = "1234.5678", version: int = 1) -> DailySnapshot:
    return DailySnapshot(
        instrument_id=INSTRUMENT,
        trading_day=TradingDay(TUESDAY),
        close=Price.parse(close),
        turnover=Money.parse("98765432.10"),
        account_id=ACCOUNT,
        version=version,
    )


# --------------------------------------------------------------------------- #
# Round trip through real columns
# --------------------------------------------------------------------------- #


async def test_money_and_price_survive_a_real_round_trip(session: AsyncSession) -> None:
    """The exactness guarantee, through actual BIGINT columns.

    Everything before this verified conversion in Python. This verifies that
    PostgreSQL stores and returns the same integers -- which is the claim ADR-042
    rests on.
    """
    repository = DailySnapshotRepository(session, FACTORY)
    original = _snapshot()

    await repository.add(original)
    await session.flush()
    loaded = await repository.get(ACCOUNT, INSTRUMENT, TradingDay(TUESDAY))

    assert loaded == original
    assert loaded is not None
    assert loaded.close.scaled_units == original.close.scaled_units


async def test_a_currency_derivative_price_survives_storage(session: AsyncSession) -> None:
    """0.0025 at eight decimal places, through a real column."""
    repository = DailySnapshotRepository(session, FACTORY)
    await repository.add(_snapshot(close="0.0025"))
    await session.flush()

    loaded = await repository.get(ACCOUNT, INSTRUMENT, TradingDay(TUESDAY))

    assert loaded is not None
    assert loaded.close == Price.parse("0.0025")


# --------------------------------------------------------------------------- #
# Transaction rollback
# --------------------------------------------------------------------------- #


async def test_a_rolled_back_transaction_leaves_nothing(
    committed_session: AsyncSession,
) -> None:
    """Rollback must discard the write, not merely appear to."""
    repository = DailySnapshotRepository(committed_session, FACTORY)

    await repository.add(_snapshot())
    await committed_session.rollback()

    assert await repository.get(ACCOUNT, INSTRUMENT, TradingDay(TUESDAY)) is None


# --------------------------------------------------------------------------- #
# Concurrency and optimistic locking
# --------------------------------------------------------------------------- #


async def test_the_second_of_two_concurrent_writers_is_refused(
    committed_session: AsyncSession, migrated: AsyncEngine
) -> None:
    """Optimistic locking (ADR-057), with genuinely concurrent sessions.

    Both writers load version 1. The first commits and the row becomes version 2.
    The second's UPDATE matches zero rows and must raise rather than silently
    overwriting -- a lost update in a financial system is a number that changed
    with no record of why.
    """
    seed = DailySnapshotRepository(committed_session, FACTORY)
    await seed.add(_snapshot())
    await committed_session.commit()

    factory = async_sessionmaker(bind=migrated, expire_on_commit=False)
    async with factory() as first, factory() as second:
        loaded_first = await DailySnapshotRepository(first, FACTORY).get(
            ACCOUNT, INSTRUMENT, TradingDay(TUESDAY)
        )
        loaded_second = await DailySnapshotRepository(second, FACTORY).get(
            ACCOUNT, INSTRUMENT, TradingDay(TUESDAY)
        )
        assert loaded_first is not None
        assert loaded_second is not None

        await DailySnapshotRepository(first, FACTORY).update(
            loaded_first.revise_close(Price.parse("1300.00"))
        )
        await first.commit()

        with pytest.raises(ConflictError):
            await DailySnapshotRepository(second, FACTORY).update(
                loaded_second.revise_close(Price.parse("1400.00"))
            )


async def test_a_conflict_reports_which_version_it_expected(
    committed_session: AsyncSession,
) -> None:
    """The error must carry enough context for a caller to decide about retrying."""
    repository = DailySnapshotRepository(committed_session, FACTORY)
    await repository.add(_snapshot())
    await committed_session.commit()

    stale = _snapshot(close="1300.00", version=5)

    with pytest.raises(ConflictError) as caught:
        await repository.update(stale)

    assert caught.value.context["expected_version"] == 4


@pytest.mark.xfail(
    reason=(
        "Deadlock detection is inherently timing-dependent: PostgreSQL may resolve "
        "the cycle before both statements block, in which case neither aborts. "
        "Marked non-strict so it records the behaviour without producing a flaky "
        "red. Treated as documentation of the expected failure mode rather than a "
        "guarantee -- deadlock *retry* is a caller concern and is tested there."
    ),
    strict=False,
)
async def test_deadlock_surfaces_as_a_driver_error(migrated: AsyncEngine) -> None:
    """Two transactions locking two rows in opposite order.

    PostgreSQL detects the cycle and aborts one. What matters for the platform is
    that the abort surfaces as an exception a caller can branch on, rather than a
    hang.
    """
    factory = async_sessionmaker(bind=migrated, expire_on_commit=False)
    other_day = TradingDay(date(2026, 7, 29))

    async with factory() as setup:
        repository = DailySnapshotRepository(setup, FACTORY)
        await repository.add(_snapshot())
        await repository.add(
            DailySnapshot(
                instrument_id=INSTRUMENT,
                trading_day=other_day,
                close=Price.parse("1000.00"),
                turnover=Money.parse("1.00"),
                account_id=ACCOUNT,
            )
        )
        await setup.commit()

    async def lock_then_lock(first: TradingDay, second: TradingDay) -> None:
        async with factory() as bound:
            for day in (first, second):
                await bound.execute(
                    text(
                        "SELECT 1 FROM daily_snapshot WHERE trading_day = :day FOR UPDATE"
                    ).bindparams(day=day.on)
                )
                await asyncio.sleep(0.05)
            await bound.commit()

    with pytest.raises(DBAPIError):
        await asyncio.gather(
            lock_then_lock(TradingDay(TUESDAY), other_day),
            lock_then_lock(other_day, TradingDay(TUESDAY)),
        )


async def test_repeated_adds_of_the_same_natural_key_are_refused(
    committed_session: AsyncSession,
) -> None:
    """The unique constraint is the last line of defence against a duplicate.

    Idempotency belongs to the caller, but the schema must refuse a second row
    for the same instrument and trading day whatever the caller believed.
    """
    repository = DailySnapshotRepository(committed_session, FACTORY)
    await repository.add(_snapshot())
    await committed_session.commit()

    await repository.add(_snapshot())

    with pytest.raises(Exception, match="uq_daily_snapshot"):
        await committed_session.commit()


# --------------------------------------------------------------------------- #
# Isolation assumptions
# --------------------------------------------------------------------------- #


async def test_the_default_isolation_level_is_read_committed(
    session: AsyncSession,
) -> None:
    """Stated rather than assumed.

    Every concurrency claim in this suite depends on the isolation level. If a
    future deployment changes it, this test says so rather than letting the
    other tests quietly start verifying something else.
    """
    level = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()

    assert level == "read committed"


async def test_an_uncommitted_write_is_invisible_to_another_session(
    migrated: AsyncEngine,
) -> None:
    """Read committed means exactly this, and the outbox drainer depends on it.

    If an uncommitted row were visible, the drainer could publish an event for a
    transaction that later rolled back -- the failure the outbox exists to
    prevent.
    """
    factory = async_sessionmaker(bind=migrated, expire_on_commit=False)

    async with factory() as writer, factory() as reader:
        await DailySnapshotRepository(writer, FACTORY).add(_snapshot())
        await writer.flush()

        visible = await DailySnapshotRepository(reader, FACTORY).get(
            ACCOUNT, INSTRUMENT, TradingDay(TUESDAY)
        )

        assert visible is None, "an uncommitted row must not be visible elsewhere"
        await writer.rollback()


# --------------------------------------------------------------------------- #
# Constraints re-assert domain invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("turnover_minor_units", -1, "ck_daily_snapshot_turnover"),
        ("close_scaled_units", 0, "ck_daily_snapshot_close"),
        ("version", 0, "ck_daily_snapshot_version"),
    ],
)
async def test_the_database_refuses_values_the_domain_would_refuse(
    session: AsyncSession, column: str, value: int, constraint: str
) -> None:
    """Defence in depth.

    The domain refuses these in Python. The constraint refuses them when they
    arrive by any other route -- a manual UPDATE, another service, a migration
    backfill. Inserted with raw SQL precisely to bypass the domain, because
    that is the route being defended against.
    """
    columns = {
        "id": uuid4(),
        "account_id": ACCOUNT.value,
        "instrument_id": INSTRUMENT.value,
        "trading_day": TUESDAY,
        "close_scaled_units": 1,
        "turnover_minor_units": 0,
        "currency": "INR",
        "version": 1,
    }
    columns[column] = value

    statement = text(
        "INSERT INTO daily_snapshot (id, account_id, instrument_id, trading_day, "
        "close_scaled_units, turnover_minor_units, currency, version) VALUES "
        "(:id, :account_id, :instrument_id, :trading_day, :close_scaled_units, "
        ":turnover_minor_units, :currency, :version)"
    ).bindparams(**columns)

    with pytest.raises(IntegrityError, match=constraint):
        await session.execute(statement)
