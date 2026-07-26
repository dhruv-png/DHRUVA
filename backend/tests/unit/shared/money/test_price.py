"""Price: fixed scale, dimensional arithmetic, and currency-derivative precision."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money import CHARGE_TO_PAISE, Money, Price, Quantity
from tests.unit.shared.money import strategies as gen

pytestmark = pytest.mark.unit


def test_scale_is_eight_and_fixed() -> None:
    """The scale is a class constant, never varied by instrument (Design Review Q1)."""
    assert Price.SCALE == 8
    assert Price.UNITS_PER_MAJOR == 10**8


@pytest.mark.parametrize(
    ("text", "places"),
    [
        ("1234.56", 2),  # NSE equity
        ("83.2525", 4),  # USD/INR futures -- the case that forced ADR-042
        ("1.00005", 5),  # FX spot convention
        ("0.00000001", 8),  # the representable floor
    ],
)
def test_every_supported_market_precision_survives_a_round_trip(text: str, places: int) -> None:
    """The regression this whole design exists to prevent.

    A Price at paise scale would silently round the USD/INR case, invisibly,
    until somebody reconciled a position.
    """
    assert Price.parse(text).as_decimal() == Decimal(text)
    assert len(text.split(".")[1]) == places


def test_a_currency_derivative_tick_is_representable() -> None:
    """USD/INR ticks at ₹0.0025. Paise scale cannot express it; this can."""
    tick = Price.parse("0.0025")

    assert tick.scaled_units == 250_000
    assert not tick.is_zero


def test_parse_rejects_precision_beyond_the_scale() -> None:
    """Nine decimal places is a belief about the value that is not true."""
    with pytest.raises(InvariantViolation, match="decimal places"):
        Price.parse("1.123456789")


@pytest.mark.parametrize("bad", [1.0, 0.5, True, None, Decimal("1")])
def test_construction_rejects_anything_but_int(bad: object) -> None:
    """Float cannot enter the pricing path (ADR-048)."""
    with pytest.raises(InvariantViolation):
        Price(bad)  # type: ignore[arg-type]  # asserting runtime rejection


@given(units=st.integers(min_value=-(10**16), max_value=10**16))
def test_repr_round_trips_through_parse(units: int) -> None:
    """A value object must be reconstructable from its representation."""
    original = Price(units)
    assert Price.parse(repr(original).split("'")[1]) == original


# --------------------------------------------------------------------------- #
# Dimensional arithmetic (ADR-043)
# --------------------------------------------------------------------------- #


@given(price=gen.price(), quantity=gen.quantity())
def test_price_times_quantity_is_money(price: Price, quantity: Quantity) -> None:
    """The defining dimensional relationship."""
    notional = price.notional(quantity, CHARGE_TO_PAISE)

    assert isinstance(notional, Money)
    assert notional.currency is price.currency


def test_notional_is_computed_exactly() -> None:
    """A figure a reviewer can check by hand."""
    assert Price.parse("100.50").notional(Quantity.shares(200), CHARGE_TO_PAISE) == (
        Money.parse("20100.00")
    )


def test_bare_multiplication_refuses_to_round() -> None:
    """``Price * Quantity`` stays expressible but never rounds silently.

    ADR-043 wants the operation to exist; ADR-044 wants no implicit rounding.
    Bare multiplication resolves both by using EXACT and raising, with a message
    naming the method that takes a policy.
    """
    with pytest.raises(InvariantViolation, match="not exact"):
        _ = Price.parse("100.005") * Quantity.shares(1)


def test_bare_multiplication_succeeds_when_exact() -> None:
    """The common case still reads naturally."""
    assert Price.parse("100.50") * Quantity.shares(200) == Money.parse("20100.00")


@given(price=gen.price(), quantity=gen.positive_quantity())
def test_notional_then_per_unit_recovers_the_price(price: Price, quantity: Quantity) -> None:
    """Round-tripping through Money loses at most one minor unit per unit held.

    Asserted as a bound rather than as equality, because money genuinely carries
    fewer decimal places than price does. Stating the bound is more useful than
    pretending the round trip is lossless.
    """
    notional = price.notional(quantity, CHARGE_TO_PAISE)
    recovered = notional.per_unit(quantity, CHARGE_TO_PAISE)

    tolerance = Price.UNITS_PER_MAJOR // 100  # one paisa expressed in price units
    assert abs(recovered.scaled_units - price.scaled_units) <= tolerance


def test_price_times_price_does_not_exist() -> None:
    """Rupees-per-unit squared is not a quantity this platform uses."""
    with pytest.raises(TypeError):
        # mypy also rejects this statically; the ignore proves it does.
        _ = Price.parse("1") * Price.parse("2")  # type: ignore[operator]


def test_price_plus_money_does_not_exist() -> None:
    """A price and an amount are dimensionally different things."""
    with pytest.raises(TypeError):
        _ = Price.parse("1") + Money.of(1)  # type: ignore[operator]  # asserting refusal


@given(a=gen.price(), b=gen.price())
def test_a_spread_between_prices_is_still_a_price(a: Price, b: Price) -> None:
    """Subtraction stays within the type."""
    assert isinstance(a - b, Price)


# --------------------------------------------------------------------------- #
# Tick alignment
# --------------------------------------------------------------------------- #


def test_round_to_tick_snaps_to_the_instrument_grid() -> None:
    """Tick size arrives from instrument metadata (S07); it is never looked up here."""
    assert Price.parse("100.03").round_to_tick(Price.parse("0.05"), CHARGE_TO_PAISE) == (
        Price.parse("100.05")
    )


@given(price=gen.price(), tick_units=st.integers(min_value=1, max_value=10**8))
def test_a_tick_aligned_price_is_always_a_multiple_of_the_tick(
    price: Price, tick_units: int
) -> None:
    """The defining property of alignment."""
    aligned = price.round_to_tick(Price(tick_units), CHARGE_TO_PAISE)

    assert aligned.scaled_units % tick_units == 0


def test_round_to_tick_rejects_a_non_positive_tick() -> None:
    """A zero tick size would be a division by zero dressed up as metadata."""
    with pytest.raises(InvariantViolation, match="tick size must be positive"):
        Price.parse("100").round_to_tick(Price.zero(), CHARGE_TO_PAISE)


# --------------------------------------------------------------------------- #
# Value-object semantics
# --------------------------------------------------------------------------- #


@given(units=st.integers(min_value=-(10**16), max_value=10**16))
def test_equal_prices_hash_equally(units: int) -> None:
    """The hash/equality contract."""
    assert hash(Price(units)) == hash(Price(units))


@given(units=st.integers(min_value=-(10**16), max_value=10**16))
def test_price_is_immutable(units: int) -> None:
    """A value object that can be mutated is not a value object."""
    with pytest.raises(AttributeError):
        Price(units)._scaled_units = 0  # asserting immutability


@given(a=gen.price(), b=gen.price())
def test_ordering_is_consistent_with_equality(a: Price, b: Price) -> None:
    """Trichotomy."""
    assert sum([a < b, a == b, a > b]) == 1


def test_price_has_no_float_conversion() -> None:
    """``float(price)`` must not exist (ADR-048)."""
    assert not hasattr(Price.parse("1"), "__float__")


@given(a=gen.price(), b=gen.price(), c=gen.price())
def test_addition_is_associative(a: Price, b: Price, c: Price) -> None:
    """Exactness means grouping cannot change the result."""
    assert (a + b) + c == a + (b + c)


@given(price=gen.price())
def test_negation_is_an_involution(price: Price) -> None:
    """Negating twice returns the original."""
    negated = -price

    assert -negated == price


@given(price=gen.price(), quantity=gen.quantity())
def test_notional_of_zero_quantity_is_zero(price: Price, quantity: Quantity) -> None:
    """Nothing bought costs nothing, at any price."""
    assume(quantity.is_zero)
    assert price.notional(quantity, CHARGE_TO_PAISE).is_zero
