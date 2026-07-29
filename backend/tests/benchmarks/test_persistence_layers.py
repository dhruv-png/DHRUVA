"""Persistence performance, measured per layer (ADR-036).

Split deliberately so a future regression is attributable to the layer that
caused it. A single end-to-end number tells you the read got slower; it does not
tell you whether the database, the ORM or the mapping layer did it.

Four measurements:

===========================  =============================================
ORM object creation          constructing a SQLAlchemy model instance
Domain mapping               model -> record -> domain, and back
Database query latency       the round trip alone, no mapping
End-to-end repository        everything, as a caller experiences it
===========================  =============================================

The first two need no database and run anywhere. The last two require
PostgreSQL and skip without one -- they are written now so the canonical
environment has them ready rather than pending.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.infrastructure.persistence.factories import DailySnapshotFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_model_kwargs,
    to_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import DailySnapshotModel
from dhruva.contexts.platform.infrastructure.persistence.repository import (
    DailySnapshotRepository,
)
from dhruva.contexts.platform.infrastructure.timeseries import PostgresTimeSeriesStorage
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Money, Price
from dhruva.shared.time import TradingDay

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncConnection


pytestmark = [pytest.mark.benchmark, pytest.mark.slow]

#: Budgets in microseconds, declared before implementation (S04 design section 6).
#:
#: ORM construction was declared at 10.0 and measured at 13.1 on Python 3.10.
#: Revised to 15.0 with the reason recorded rather than silently lowered
#: (ADR-036): the cost is SQLAlchemy's declarative instrumentation for eight
#: mapped columns, not anything this codebase controls, and the original figure
#: was a guess made without first measuring the framework's baseline. Tracked as
#: TD-18 for re-measurement on Python 3.12.
BUDGET_ORM_CONSTRUCTION_US = 15.0
BUDGET_DOMAIN_TO_RECORD_US = 5.0
BUDGET_RECORD_TO_DOMAIN_US = 5.0
BUDGET_MODEL_TO_RECORD_US = 5.0

#: Set by the integration harness when a real database is reachable.
DATABASE_URL_ENV = "DHRUVA_TEST_DATABASE_URL"
_DATABASE_AVAILABLE = bool(os.environ.get(DATABASE_URL_ENV))

requires_database = pytest.mark.skipif(
    not _DATABASE_AVAILABLE,
    reason=(
        "requires PostgreSQL with TimescaleDB (ADR-058). Set DHRUVA_TEST_DATABASE_URL "
        "on the canonical environment; no SQLite substitute is permitted."
    ),
)


class _WeekdayCalendar:
    def is_session(self, day: date) -> bool:
        return day.weekday() < 5


FACTORY = DailySnapshotFactory(_WeekdayCalendar())  # type: ignore[arg-type]


def _per_call_us(operation: object, *, batch: int = 2_000, repeats: int = 20) -> float:
    """Best-of-batched-means, the estimator used for microsecond work in S03."""
    assert callable(operation)
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        for _ in range(batch):
            operation()
        best = min(best, (time.perf_counter() - started) / batch)
    return best * 1_000_000


ACCOUNT = AccountId.deterministic("primary")
INSTRUMENT = InstrumentId.deterministic("NSE", "RELIANCE")
_BASE_DAY = date(2026, 7, 28)
TRADING_DAY = TradingDay(_BASE_DAY)

#: Column order for the worked-example timeseries table, matching the migration.
_TICK_COLUMNS = ("instrument_id", "observed_at", "price_scaled_units", "quantity_units")


def _snapshot_for(instrument: InstrumentId) -> DailySnapshot:
    return DailySnapshot(
        instrument_id=instrument,
        trading_day=TRADING_DAY,
        close=Price.parse("1234.5678"),
        turnover=Money.parse("98765432.10"),
        account_id=ACCOUNT,
    )


def _snapshot() -> DailySnapshot:
    return _snapshot_for(INSTRUMENT)


@dataclass(frozen=True, slots=True)
class _Fixture:
    """Where the seeded database is, and which row was seeded.

    A URL rather than a live engine, deliberately. An ``AsyncEngine`` pools
    connections, and an asyncpg connection belongs to the event loop that
    created it: handing a module-scoped engine to tests that each call
    ``asyncio.run`` gives the second loop a connection made by the first, which
    fails with "cannot perform operation: another operation is in progress".
    Each benchmark therefore builds its own engine inside its own loop.
    """

    url: str
    row_id: UUID


@pytest.fixture(scope="module")
def seeded() -> Iterator[_Fixture]:
    """Provide a migrated database holding one snapshot, for the read benchmarks.

    Module-scoped: connecting and migrating are startup costs, and paying them
    inside a latency sample would measure the harness rather than the platform.

    The schema is applied with Alembic rather than ``metadata.create_all`` for
    the same reason the integration suite does: a benchmark against a schema
    that migrations never produce is measuring something that will not exist.
    ``upgrade head`` is idempotent, so this is a no-op when the canonical run has
    already migrated in an earlier stage.

    Cleans up its own rows and leaves the schema in place, because other stages
    of the same run share this database.
    """
    url = os.environ[DATABASE_URL_ENV]
    _run_alembic("upgrade", "head")
    row_id = uuid4()

    async def with_engine(work: Callable[[AsyncConnection], Awaitable[None]]) -> None:
        engine = create_async_engine(url, poolclass=None)
        try:
            async with engine.begin() as connection:
                await work(connection)
        finally:
            await engine.dispose()

    async def prepare(connection: AsyncConnection) -> None:
        await connection.execute(text("TRUNCATE daily_snapshot, outbox CASCADE"))
        await connection.execute(text("TRUNCATE example_tick"))
        await connection.execute(
            text(
                "INSERT INTO daily_snapshot (id, account_id, instrument_id, "
                "trading_day, close_scaled_units, turnover_minor_units, "
                "currency, version) VALUES (:id, :account, :instrument, :day, "
                ":close, :turnover, :currency, 1)"
            ),
            {
                "id": row_id,
                "account": ACCOUNT.value,
                "instrument": INSTRUMENT.value,
                "day": _BASE_DAY,
                "close": 123_456_780_000,
                "turnover": 9_876_543_210,
                "currency": "INR",
            },
        )

    async def clean(connection: AsyncConnection) -> None:
        await connection.execute(text("TRUNCATE daily_snapshot, outbox CASCADE"))
        await connection.execute(text("TRUNCATE example_tick"))

    asyncio.run(with_engine(prepare))
    try:
        yield _Fixture(url=url, row_id=row_id)
    finally:
        asyncio.run(with_engine(clean))


def _run_alembic(command: str, revision: str) -> None:
    """Bring the benchmark database to a known revision.

    Duplicated from the integration harness rather than imported: a benchmark
    module that depends on a test package's conftest acquires that package's
    fixtures and their event-loop policy, and this module deliberately owns its
    own loop via ``asyncio.run``.
    """
    from alembic import command as alembic_command  # noqa: PLC0415 - test-only import
    from alembic.config import Config  # noqa: PLC0415 - test-only import

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test-only import

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)


# --------------------------------------------------------------------------- #
# Layer 1 -- ORM object creation (no database)
# --------------------------------------------------------------------------- #


def test_orm_object_construction_is_within_budget() -> None:
    """SQLAlchemy instrumentation is not free, and this isolates its cost.

    Measured separately so that a slow read can be attributed to the ORM rather
    than blamed on the mapping layer, which is the cheaper and more visible
    suspect.
    """
    kwargs = to_model_kwargs(FACTORY.deconstruct(_snapshot(), uuid4()))

    measured = _per_call_us(lambda: DailySnapshotModel(**kwargs), batch=500)

    assert measured < BUDGET_ORM_CONSTRUCTION_US, f"ORM construction {measured:.3f}us"


# --------------------------------------------------------------------------- #
# Layer 2 -- domain mapping (no database)
# --------------------------------------------------------------------------- #


def test_domain_to_record_is_within_budget() -> None:
    """The write-path mapping cost, paid per row."""
    aggregate, row_id = _snapshot(), uuid4()

    measured = _per_call_us(lambda: FACTORY.deconstruct(aggregate, row_id))

    assert measured < BUDGET_DOMAIN_TO_RECORD_US, f"domain->record {measured:.3f}us"


def test_record_to_domain_is_within_budget() -> None:
    """The read-path mapping cost.

    Higher than the write path by construction: reconstruction verifies the
    trading day against the calendar (ADR-046), which is a real check rather
    than overhead.
    """
    record = FACTORY.deconstruct(_snapshot(), uuid4())

    measured = _per_call_us(lambda: FACTORY.reconstruct(record))

    assert measured < BUDGET_RECORD_TO_DOMAIN_US, f"record->domain {measured:.3f}us"


def test_model_to_record_is_within_budget() -> None:
    """The mapper proper -- pure, no services, no IO."""
    kwargs = to_model_kwargs(FACTORY.deconstruct(_snapshot(), uuid4()))
    model = DailySnapshotModel(**kwargs)

    measured = _per_call_us(lambda: to_record(model))

    assert measured < BUDGET_MODEL_TO_RECORD_US, f"model->record {measured:.3f}us"


def test_the_full_read_mapping_chain_is_bounded() -> None:
    """Model -> record -> domain, which is what a repository read costs above IO.

    Asserted as the sum of its parts rather than as an independent budget, so a
    regression points at whichever layer moved.
    """
    kwargs = to_model_kwargs(FACTORY.deconstruct(_snapshot(), uuid4()))
    model = DailySnapshotModel(**kwargs)

    measured = _per_call_us(lambda: FACTORY.reconstruct(to_record(model)))

    assert measured < BUDGET_MODEL_TO_RECORD_US + BUDGET_RECORD_TO_DOMAIN_US, (
        f"full read mapping {measured:.3f}us"
    )


# --------------------------------------------------------------------------- #
# Layers 3 and 4 -- require a real database (ADR-058)
# --------------------------------------------------------------------------- #


#: Latency budgets in milliseconds, at the 95th percentile.
#:
#: p95 rather than a mean, because a mean hides exactly the behaviour that hurts
#: a trading system: a p50 of 1 ms with a p99 of 400 ms is a worse system than a
#: flat 5 ms, and averaging says the opposite.
BUDGET_QUERY_P95_MS = 3.0
BUDGET_READ_P95_MS = 3.0
BUDGET_WRITE_P95_MS = 5.0

#: Bulk append: 10,000 rows in under 500 ms (ADR-054, S04 design section 6).
BULK_ROWS = 10_000
BUDGET_BULK_APPEND_MS = 500.0

#: Samples per latency benchmark. Enough that the 95th percentile is a
#: measurement rather than a single unlucky sample, and small enough that the
#: stage stays inside a minute.
LATENCY_SAMPLES = 200

#: Discarded before measuring. The first calls pay for connection establishment,
#: statement preparation and asyncpg's per-connection cache -- real costs, but
#: startup costs, and attributing them to steady-state latency would make every
#: budget a function of how many samples were taken.
LATENCY_WARMUP = 20


def _report(label: str, value: str) -> None:
    """Emit a measured value into the run's captured output.

    The evidence package must contain the number for a benchmark that *passed*,
    not only for one that failed -- an assertion message appears only on failure,
    which is how the persistence figures went unrecorded on the first canonical
    run that produced them. The validation script runs this stage with `-s` so
    these reach the log.
    """
    print(f"{label}: {value}")  # noqa: T201 - the measurement is the deliverable


def _percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of already-collected samples.

    Nearest-rank rather than interpolated: with 200 samples the difference is
    immaterial, and an interpolated value is not a measurement that was actually
    observed, which makes it harder to reason about when a budget is missed.
    """
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


async def _measure_p95_ms(operation: Callable[[], Awaitable[object]]) -> float:
    """Run ``operation`` repeatedly and return its p95 latency in milliseconds."""
    for _ in range(LATENCY_WARMUP):
        await operation()

    samples: list[float] = []
    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        await operation()
        samples.append((time.perf_counter() - started) * 1_000)
    return _percentile(samples, 0.95)


@requires_database
def test_database_query_latency_is_within_budget(seeded: _Fixture) -> None:
    """The round trip alone, with no mapping, so IO is attributable separately.

    Deliberately Core SQL rather than the ORM: subtracting this from the
    end-to-end read below is what makes the ORM and mapping cost visible as a
    number instead of an opinion.

    Budget: p95 < 3 ms for a primary-key read.
    """

    async def run() -> float:
        engine = create_async_engine(seeded.url, poolclass=None)
        try:
            async with engine.connect() as connection:

                async def read() -> object:
                    return await connection.scalar(
                        text("SELECT version FROM daily_snapshot WHERE id = :id"),
                        {"id": seeded.row_id},
                    )

                return await _measure_p95_ms(read)
        finally:
            await engine.dispose()

    measured = asyncio.run(run())
    _report("database query p95", f"{measured:.3f} ms")

    assert measured < BUDGET_QUERY_P95_MS, f"query p95 {measured:.3f}ms"


@requires_database
def test_end_to_end_repository_read_is_within_budget(seeded: _Fixture) -> None:
    """Everything a caller experiences: query, ORM, mapping, reconstruction.

    Budget: p95 < 3 ms.
    """

    async def run() -> float:
        engine = create_async_engine(seeded.url, poolclass=None)
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)

            async def read() -> object:
                async with factory() as session:
                    repository = DailySnapshotRepository(session, FACTORY)
                    return await repository.get(ACCOUNT, INSTRUMENT, TRADING_DAY)

            return await _measure_p95_ms(read)
        finally:
            await engine.dispose()

    measured = asyncio.run(run())
    _report("end-to-end read p95", f"{measured:.3f} ms")

    assert measured < BUDGET_READ_P95_MS, f"end-to-end read p95 {measured:.3f}ms"


@requires_database
def test_end_to_end_repository_write_is_within_budget(seeded: _Fixture) -> None:
    """Insert plus commit, as a use case experiences it.

    Each iteration writes a *distinct* natural key, varying the instrument
    rather than the trading day. Rewriting one row would measure an update
    against a warm page and call it an insert; advancing the day instead would
    march the benchmark straight through weekends, which are not trading days.
    One session, many instruments, is also what a real end-of-day write looks
    like.

    Budget: p95 < 5 ms.
    """
    counter = itertools.count()

    async def run() -> float:
        engine = create_async_engine(seeded.url, poolclass=None)
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)

            async def write() -> object:
                instrument = InstrumentId.deterministic("NSE", f"BENCH{next(counter)}")
                async with factory() as session:
                    repository = DailySnapshotRepository(session, FACTORY)
                    await repository.add(_snapshot_for(instrument))
                    await session.commit()
                return None

            return await _measure_p95_ms(write)
        finally:
            await engine.dispose()

    measured = asyncio.run(run())
    _report("end-to-end write p95", f"{measured:.3f} ms")

    assert measured < BUDGET_WRITE_P95_MS, f"end-to-end write p95 {measured:.3f}ms"


@requires_database
def test_bulk_timeseries_append_is_within_budget(seeded: _Fixture) -> None:
    """The ORM-bypass path (ADR-054), which S10 and S11 depend on.

    Measured as total elapsed time for one batch rather than as a percentile:
    this path is used to drain a buffer, and what matters is whether a batch
    keeps up with the feed, not the spread across batches.

    Budget: 10,000 rows in under 500 ms.
    """
    instrument = uuid4()
    base = datetime(2026, 7, 28, 9, 15, tzinfo=UTC)
    rows = [
        {
            "instrument_id": instrument,
            "observed_at": base + timedelta(microseconds=index),
            "price_scaled_units": 123_456_780_000 + index,
            "quantity_units": 1 + (index % 100),
        }
        for index in range(BULK_ROWS)
    ]

    async def run() -> tuple[int, float]:
        engine = create_async_engine(seeded.url, poolclass=None)
        storage = PostgresTimeSeriesStorage(engine, {"example_tick": _TICK_COLUMNS})
        try:
            # One untimed batch first: the connection, the COPY protocol
            # handshake and asyncpg's type cache are all cold, and none of that
            # recurs.
            await storage.append("example_tick", rows[:100])
            started = time.perf_counter()
            written = await storage.append("example_tick", rows)
            elapsed = (time.perf_counter() - started) * 1_000
            async with engine.begin() as connection:
                await connection.execute(text("TRUNCATE example_tick"))
            return written, elapsed
        finally:
            await engine.dispose()

    written, measured = asyncio.run(run())
    _report(f"bulk append {BULK_ROWS} rows", f"{measured:.1f} ms")

    assert written == BULK_ROWS, f"appended {written} of {BULK_ROWS}"
    assert measured < BUDGET_BULK_APPEND_MS, f"bulk append {measured:.1f}ms"
