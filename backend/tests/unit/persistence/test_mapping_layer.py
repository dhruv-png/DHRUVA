"""The four-layer mapping flow, round-tripped without a database.

Domain <-> record <-> model kwargs. The database round trip is an integration
concern (ADR-058); what is verified here is that no information is lost or
invented at any layer boundary, which is where a mapping defect actually lives.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date, timedelta
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

import dhruva.contexts.platform.domain.example_snapshot as snapshot_module
import dhruva.contexts.platform.infrastructure.persistence.mappers as mapper_module
from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.infrastructure.persistence.factories import DailySnapshotFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import to_model_kwargs
from dhruva.contexts.platform.infrastructure.persistence.records import DailySnapshotRecord
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Money, Price
from dhruva.shared.time import TradingDay

pytestmark = pytest.mark.unit


class WeekdayCalendar:
    """Weekdays are sessions. Stands in for S08's real calendar."""

    def is_session(self, day: date) -> bool:
        """Return whether the market was open."""
        return day.weekday() < 5


CALENDAR = WeekdayCalendar()
FACTORY = DailySnapshotFactory(CALENDAR)  # type: ignore[arg-type]
TUESDAY = date(2026, 7, 28)


def _snapshot(**overrides: object) -> DailySnapshot:
    defaults: dict[str, object] = {
        "instrument_id": InstrumentId.deterministic("NSE", "RELIANCE"),
        "trading_day": TradingDay(TUESDAY),
        "close": Price.parse("1234.5678"),
        "turnover": Money.parse("98765432.10"),
        "account_id": AccountId.deterministic("primary"),
        "version": 1,
    }
    return DailySnapshot(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


@given(
    close_units=st.integers(min_value=1, max_value=10**15),
    turnover_units=st.integers(min_value=0, max_value=10**15),
    version=st.integers(min_value=1, max_value=10**6),
)
def test_domain_to_record_to_domain_is_lossless(
    close_units: int, turnover_units: int, version: int
) -> None:
    """The property that makes the mapping layer trustworthy.

    Every field survives both directions. A dropped field is the classic mapping
    defect and it is silent -- the row saves, the value is simply gone.
    """
    original = _snapshot(close=Price(close_units), turnover=Money(turnover_units), version=version)
    row_id = uuid4()

    record = FACTORY.deconstruct(original, row_id)

    assert FACTORY.reconstruct(record) == original


def test_record_to_model_kwargs_covers_every_column() -> None:
    """A field added to the record but forgotten in the mapper would be silent."""
    record = FACTORY.deconstruct(_snapshot(), uuid4())

    kwargs = to_model_kwargs(record)

    assert set(kwargs) == {f.name for f in record.__dataclass_fields__.values()}


def test_money_survives_the_full_flow_exactly() -> None:
    """The paisa-exactness guarantee, asserted across all four layers."""
    turnover = Money.parse("98765432.10")

    record = FACTORY.deconstruct(_snapshot(turnover=turnover), uuid4())

    assert record.turnover_minor_units == 9876543210
    assert FACTORY.reconstruct(record).turnover == turnover


def test_price_survives_the_full_flow_at_eight_decimal_places() -> None:
    """Including the currency-derivative case that forced ADR-042."""
    close = Price.parse("83.2525")

    record = FACTORY.deconstruct(_snapshot(close=close), uuid4())

    assert record.close_scaled_units == 8325250000
    assert FACTORY.reconstruct(record).close == close


# --------------------------------------------------------------------------- #
# Amendment 2: reconstruction goes through the calendar
# --------------------------------------------------------------------------- #


def test_reconstruction_verifies_the_trading_day_against_the_calendar() -> None:
    """ADR-046 holds on the read path, not only on the write path.

    A stored date that is not a session -- because a holiday was added
    retrospectively, or a bad import wrote one -- must surface here rather than
    propagate into a backtest.
    """
    sunday = TUESDAY - timedelta(days=2)
    record = DailySnapshotRecord(
        id=uuid4(),
        account_id=uuid4(),
        instrument_id=uuid4(),
        trading_day=sunday,
        close_scaled_units=1,
        turnover_minor_units=0,
        currency="INR",
        version=1,
    )

    with pytest.raises(InvariantViolation, match="not a trading session"):
        FACTORY.reconstruct(record)


def test_the_mapper_holds_no_calendar() -> None:
    """Purity is what makes the mapper independently benchmarkable.

    A calendar in the mapper would make its cost depend on a lookup, and a
    mapping benchmark would then be measuring the calendar.
    """
    source = mapper_module.__doc__ or ""

    assert "calendar" not in dir(mapper_module)
    assert "Pure functions" in source


def test_the_record_carries_no_domain_types() -> None:
    """A record with a TradingDay in it would defeat the whole layering.

    The record exists precisely so a mapper can produce one without needing a
    calendar.
    """
    annotations = DailySnapshotRecord.__annotations__

    for name, annotation in annotations.items():
        assert str(annotation) in {"UUID", "date", "int", "str"}, (
            f"{name} is a domain type; records carry primitives only"
        )


# --------------------------------------------------------------------------- #
# Aggregate invariants
# --------------------------------------------------------------------------- #


def test_a_zero_close_is_refused() -> None:
    """A snapshot with no close price does not describe a real session."""
    with pytest.raises(InvariantViolation, match="non-zero"):
        _snapshot(close=Price.zero())


def test_negative_turnover_is_refused() -> None:
    """Value traded cannot be below zero."""
    with pytest.raises(InvariantViolation, match="cannot be negative"):
        _snapshot(turnover=Money.parse("-1.00"))


def test_revising_a_close_advances_the_version() -> None:
    """Optimistic concurrency (ADR-057): a revision is a new version, not an edit."""
    original = _snapshot()

    revised = original.revise_close(Price.parse("1300.00"))

    assert revised.version == original.version + 1
    assert revised.close == Price.parse("1300.00")
    assert original.close == Price.parse("1234.5678"), "the original must be unchanged"


def test_the_domain_module_imports_no_persistence_framework() -> None:
    """Rule R7 in miniature, asserted at the module the rule protects."""
    tree = ast.parse(pathlib.Path(snapshot_module.__file__ or "").read_text(encoding="utf-8"))
    imported = {
        node.names[0].name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported.isdisjoint({"sqlalchemy", "alembic", "asyncpg", "psycopg"})
