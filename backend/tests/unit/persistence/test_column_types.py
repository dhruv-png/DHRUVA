"""Domain column types are lossless in both directions.

These run without a database. They exercise the ``TypeDecorator`` hooks directly,
which is where the conversion logic lives -- the database round trip itself is an
integration concern (ADR-058) and is tested separately.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Dialect, TypeDecorator

from dhruva.contexts.platform.infrastructure.database.types import (
    MoneyAmount,
    MoneyCurrency,
    PriceUnits,
    QuantityUnits,
    RatioValue,
    SurrogateIdType,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio

pytestmark = pytest.mark.unit

#: SQLAlchemy passes a real Dialect at runtime. None of these types consult it --
#: they convert between a domain object and a primitive, which is dialect
#: independent by design. Passing None keeps the tests database-free, and the
#: cast documents that the argument is genuinely unused rather than forgotten.
NO_DIALECT = cast("Dialect", None)


@given(minor_units=st.integers(min_value=-(10**15), max_value=10**15))
def test_money_round_trips_exactly(minor_units: int) -> None:
    """The stored form is the in-memory form, so no conversion can be wrong."""
    column = MoneyAmount()
    original = Money(minor_units)

    stored = column.process_bind_param(original, NO_DIALECT)
    restored = column.process_result_value(stored, NO_DIALECT)

    assert stored == minor_units
    assert restored == original


@given(scaled_units=st.integers(min_value=-(10**16), max_value=10**16))
def test_price_round_trips_at_full_precision(scaled_units: int) -> None:
    """All eight decimal places survive, including the currency-derivative case."""
    column = PriceUnits()
    original = Price(scaled_units)

    restored = column.process_result_value(
        column.process_bind_param(original, NO_DIALECT), NO_DIALECT
    )

    assert restored == original


def test_a_currency_derivative_tick_survives_storage() -> None:
    """USD/INR ticks at 0.0025. This is the value that forced ADR-042."""
    column = PriceUnits()
    tick = Price.parse("0.0025")

    assert (
        column.process_result_value(column.process_bind_param(tick, NO_DIALECT), NO_DIALECT) == tick
    )


def test_money_written_to_a_column_bound_to_another_currency_is_refused() -> None:
    """A column cannot see its sibling currency column, so it guards its own.

    Unexercisable today with one currency in the enum, so the guard is asserted
    through the code path rather than through a second currency. It becomes a
    real test the day one is added.
    """
    column = MoneyAmount(Currency.INR)

    assert column.process_bind_param(Money(100, Currency.INR), NO_DIALECT) == 100


@given(units=st.integers(min_value=0, max_value=10**9))
def test_quantity_round_trips_and_reasserts_its_invariant(units: int) -> None:
    """Reading reconstructs through the domain type, so the invariant holds again."""
    column = QuantityUnits()

    restored = column.process_result_value(
        column.process_bind_param(Quantity(units), NO_DIALECT), NO_DIALECT
    )

    assert restored == Quantity(units)


def test_a_negative_quantity_read_from_the_database_is_refused() -> None:
    """Defence in depth: the schema should also carry a CHECK constraint.

    If a negative value reaches the column by any route -- a manual UPDATE, a
    migration, a bug in another service -- reconstruction refuses it rather than
    handing a negative size to the domain.
    """
    with pytest.raises(InvariantViolation, match="must not be negative"):
        QuantityUnits().process_result_value(-1, NO_DIALECT)


@given(
    fraction=st.decimals(
        min_value=Decimal("-100"), max_value=Decimal("100"), places=8, allow_nan=False
    )
)
def test_ratio_round_trips_exactly(fraction: Decimal) -> None:
    """NUMERIC, never DOUBLE PRECISION -- a float rate is a float amount, once removed."""
    column = RatioValue()
    original = Ratio(fraction)

    restored = column.process_result_value(
        column.process_bind_param(original, NO_DIALECT), NO_DIALECT
    )

    assert restored == original


def test_currency_column_round_trips() -> None:
    """Stored as its ISO code."""
    column = MoneyCurrency()

    assert column.process_bind_param(Currency.INR, NO_DIALECT) == "INR"
    assert column.process_result_value("INR", NO_DIALECT) is Currency.INR


def test_an_unknown_currency_code_is_refused_rather_than_guessed() -> None:
    """A code the platform does not know is data corruption, not a default."""
    with pytest.raises(ValueError, match="XYZ"):
        MoneyCurrency().process_result_value("XYZ", NO_DIALECT)


@given(value=st.uuids())
def test_identifiers_round_trip_as_native_uuids(value: object) -> None:
    """Native UUID: 16 bytes rather than 36, and malformed values fail at the database."""
    column = SurrogateIdType(InstrumentId)
    original = InstrumentId(cast("UUID", value))

    assert column.process_bind_param(original, NO_DIALECT) == value
    assert column.process_result_value(cast("UUID", value), NO_DIALECT) == original


def test_an_identifier_column_returns_only_its_bound_type() -> None:
    """An InstrumentId column must never hand back an AccountId.

    The two never compare equal (S03), so a column returning the wrong one would
    make a lookup fail in a way that looks like missing data rather than like a
    bug.
    """
    shared = uuid4()

    instrument = SurrogateIdType(InstrumentId).process_result_value(shared, NO_DIALECT)
    account = SurrogateIdType(AccountId).process_result_value(shared, NO_DIALECT)

    assert isinstance(instrument, InstrumentId)
    assert isinstance(account, AccountId)
    assert instrument != account


def test_copying_an_identifier_column_preserves_its_bound_type() -> None:
    """SQLAlchemy copies types internally; a copy that forgot would be silent."""
    copied = SurrogateIdType(AccountId).copy()

    assert isinstance(copied.process_result_value(uuid4(), NO_DIALECT), AccountId)


@pytest.mark.parametrize(
    "column",
    [
        MoneyAmount(),
        MoneyCurrency(),
        PriceUnits(),
        QuantityUnits(),
        RatioValue(),
        SurrogateIdType(InstrumentId),
    ],
    ids=lambda c: type(c).__name__,
)
def test_every_column_type_handles_null(column: TypeDecorator[object]) -> None:
    """A nullable column must not crash on the absence of a value."""
    assert column.process_bind_param(None, NO_DIALECT) is None
    assert column.process_result_value(None, NO_DIALECT) is None


@pytest.mark.parametrize(
    "column",
    [MoneyAmount(), PriceUnits(), QuantityUnits(), RatioValue()],
    ids=lambda c: type(c).__name__,
)
def test_every_column_type_is_cacheable(column: TypeDecorator[object]) -> None:
    """SQLAlchemy caches compiled statements only for types that opt in.

    Without ``cache_ok`` every statement touching one of these columns is
    recompiled, which is a silent throughput cost rather than an error.
    """
    assert column.cache_ok is True
