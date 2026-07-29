"""The ORM-bypass write path, against a real PostgreSQL (ADR-054, ADR-058).

``COPY`` behaves differently from ``INSERT`` in ways a fake cannot reproduce:
type coercion happens in the binary protocol, constraint violations surface from
the driver rather than from SQLAlchemy, and the whole batch is one statement. A
mock of this class would assert that the code calls the method it calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.timeseries import (
    PostgresTimeSeriesStorage,
    UnknownTimeSeriesTableError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

COLUMNS = ("instrument_id", "observed_at", "price_scaled_units", "quantity_units")
BASE = datetime(2026, 7, 28, 9, 15, tzinfo=UTC)


def _storage(engine: AsyncEngine) -> PostgresTimeSeriesStorage:
    return PostgresTimeSeriesStorage(engine, {"example_tick": COLUMNS})


def _tick(instrument: object, index: int = 0, *, quantity: int = 10) -> dict[str, object]:
    return {
        "instrument_id": instrument,
        "observed_at": BASE + timedelta(microseconds=index),
        "price_scaled_units": 123_456_780_000 + index,
        "quantity_units": quantity,
    }


async def test_appended_rows_are_readable_with_their_values_intact(
    migrated: AsyncEngine, truncated_after_test: None
) -> None:
    """The round trip that matters: what went in is what comes out.

    BIGINT price at eight-decimal scale specifically, because a driver that
    silently narrowed it to a float would lose the precision ADR-042 exists to
    protect and would still look like it worked.
    """
    instrument = uuid4()
    storage = _storage(migrated)

    written = await storage.append("example_tick", [_tick(instrument, i) for i in range(3)])

    assert written == 3
    async with migrated.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT price_scaled_units, quantity_units FROM example_tick "
                    "WHERE instrument_id = :id ORDER BY observed_at"
                ),
                {"id": instrument},
            )
        ).all()
    assert [tuple(row) for row in rows] == [
        (123_456_780_000, 10),
        (123_456_780_001, 10),
        (123_456_780_002, 10),
    ]


async def test_an_empty_batch_is_a_no_op(
    migrated: AsyncEngine, truncated_after_test: None
) -> None:
    """A caller draining an empty buffer should not need a guard."""
    assert await _storage(migrated).append("example_tick", []) == 0


async def test_a_table_off_the_allowlist_is_refused(
    migrated: AsyncEngine, truncated_after_test: None
) -> None:
    """The allowlist is the reason a table name may be interpolated at all."""
    with pytest.raises(UnknownTimeSeriesTableError, match="allowlist"):
        await _storage(migrated).append("daily_snapshot", [_tick(uuid4())])


@pytest.mark.parametrize("name", ["example_tick; DROP TABLE daily_snapshot", "Example_Tick", ""])
async def test_an_identifier_that_is_not_a_plain_name_is_refused_at_construction(
    migrated: AsyncEngine, name: str
) -> None:
    """Rejected rather than escaped, and rejected at wiring time.

    Includes the injection attempt explicitly: this is the one path where an
    identifier reaches PostgreSQL as text, so the check that makes that safe
    deserves a test that would fail loudly if someone removed it.
    """
    with pytest.raises(UnknownTimeSeriesTableError, match="plain identifier"):
        PostgresTimeSeriesStorage(migrated, {name: COLUMNS})


async def test_a_row_missing_a_column_is_refused_before_any_write(
    migrated: AsyncEngine, truncated_after_test: None
) -> None:
    """A partial row on a bulk path is a silent NULL discovered 10,000 rows later.

    Asserted to write *nothing*: the batch is validated before COPY begins, so a
    bad row at position two does not leave row one committed.
    """
    instrument = uuid4()
    good = _tick(instrument, 0)
    partial = {key: value for key, value in _tick(instrument, 1).items() if key != "quantity_units"}

    with pytest.raises(UnknownTimeSeriesTableError, match="missing a required"):
        await _storage(migrated).append("example_tick", [good, partial])

    async with migrated.connect() as connection:
        remaining = await connection.scalar(text("SELECT count(*) FROM example_tick"))
    assert remaining == 0


async def test_the_check_constraint_still_applies_on_this_path(
    migrated: AsyncEngine, truncated_after_test: None
) -> None:
    """Bypassing the mapper does not mean bypassing the schema.

    The domain is not consulted on this path (ADR-054), so the database
    constraint is the only thing standing between a bad tick and storage. It
    holds, and this is the test that says so.
    """
    with pytest.raises(Exception, match="ck_example_tick_quantity"):
        await _storage(migrated).append("example_tick", [_tick(uuid4(), quantity=0)])
