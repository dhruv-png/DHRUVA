"""Quantity, Side and SignedQuantity (ADR-045)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money import Quantity, Side, SignedQuantity
from tests.unit.shared.money import strategies as gen

pytestmark = pytest.mark.unit


@given(units=st.integers(min_value=1, max_value=10**9))
def test_quantity_refuses_to_be_negative(units: int) -> None:
    """The whole point of ADR-045.

    Encoding "sell" as a negative number works until somebody forgets it, and
    the failure mode is a position with the wrong sign, which reconciles cleanly
    against nothing.
    """
    with pytest.raises(InvariantViolation, match="must not be negative"):
        Quantity.shares(-units)


@pytest.mark.parametrize("bad", [1.0, 1.5, True, None, "10"])
def test_quantity_rejects_non_integers(bad: object) -> None:
    """``Quantity(True)`` meaning one share is not a reading anyone intends."""
    with pytest.raises(InvariantViolation):
        Quantity(bad)  # type: ignore[arg-type]  # asserting runtime rejection


def test_lots_requires_an_explicit_lot_size() -> None:
    """Three lots of NIFTY is 225 units, not 3. The gap is a 25x position error."""
    assert Quantity.lots(3, lot_size=75).units == 225


def test_lots_has_no_default_lot_size() -> None:
    """A default would be a guess about instrument metadata this layer does not have."""
    with pytest.raises(TypeError):
        Quantity.lots(3)  # type: ignore[call-arg]


@given(lots=st.integers(1, 10_000), lot_size=st.integers(1, 10_000))
def test_lots_round_trip(lots: int, lot_size: int) -> None:
    """Converting to units and back recovers the lot count."""
    assert Quantity.lots(lots, lot_size=lot_size).in_lots(lot_size=lot_size) == lots


def test_a_partial_lot_is_refused_rather_than_rounded() -> None:
    """The exchange would reject the order; better to fail here than there."""
    with pytest.raises(InvariantViolation, match="whole number of lots"):
        Quantity.shares(100).in_lots(lot_size=75)


@given(a=gen.quantity(), b=gen.quantity())
def test_addition_is_commutative(a: Quantity, b: Quantity) -> None:
    """A property, not three examples."""
    assert a + b == b + a


@given(a=gen.quantity(), b=gen.quantity())
def test_subtraction_refuses_to_go_negative(a: Quantity, b: Quantity) -> None:
    """Not clamped at zero.

    Subtracting more than is held is a logic error upstream, and silently
    producing zero would hide it at exactly the moment it matters.
    """
    if a >= b:
        assert (a - b).units == a.units - b.units
    else:
        with pytest.raises(InvariantViolation, match="negative quantity"):
            _ = a - b


def test_side_sign_is_the_single_conversion_point() -> None:
    """Every direction-to-number conversion goes through here."""
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY


@given(quantity=gen.quantity(), side=gen.side())
def test_signing_a_quantity_preserves_magnitude(quantity: Quantity, side: Side) -> None:
    """Direction is applied without altering size."""
    signed = quantity.signed(side)

    assert signed.magnitude == quantity
    assert signed.units == quantity.units * side.sign


@given(quantity=gen.positive_quantity(), side=gen.side())
def test_a_signed_quantity_reports_its_side(quantity: Quantity, side: Side) -> None:
    """Round-trip through the signed representation."""
    assert SignedQuantity.of(quantity, side).side is side


def test_a_zero_delta_has_no_side() -> None:
    """``None`` rather than a default.

    A zero delta genuinely has no direction, and inventing one would be the same
    category of mistake this module exists to prevent.
    """
    assert SignedQuantity.zero().side is None


@given(a=st.integers(-(10**6), 10**6), b=st.integers(-(10**6), 10**6))
def test_signed_deltas_net_correctly(a: int, b: int) -> None:
    """Netting fills into a position is addition of signed deltas."""
    assert (SignedQuantity(a) + SignedQuantity(b)).units == a + b


@given(units=st.integers(-(10**6), 10**6))
def test_negation_reverses_a_delta(units: int) -> None:
    """The delta that closes a position is the negation of the one that opened it."""
    delta = SignedQuantity(units)

    assert (delta + (-delta)).is_zero


@given(units=st.integers(0, 10**9))
def test_quantity_is_immutable_and_hashable(units: int) -> None:
    """Value-object semantics."""
    quantity = Quantity(units)

    assert hash(quantity) == hash(Quantity(units))
    assert {quantity: "v"}[Quantity(units)] == "v"
    with pytest.raises(AttributeError):
        quantity._units = 0  # asserting immutability


@given(units=st.integers(-(10**6), 10**6))
def test_signed_quantity_is_immutable_and_hashable(units: int) -> None:
    """Value-object semantics for the signed variant too."""
    delta = SignedQuantity(units)

    assert hash(delta) == hash(SignedQuantity(units))
    with pytest.raises(AttributeError):
        delta._units = 0  # asserting immutability


def test_quantity_and_signed_quantity_are_distinct_types() -> None:
    """They must not be interchangeable.

    An order size can never be negative; a position delta can. Sharing a type
    would mean every function receiving one has to establish which it holds.
    """
    assert Quantity(5) != SignedQuantity(5)
    with pytest.raises(TypeError):
        # mypy also rejects this statically; the ignore proves it does.
        _ = Quantity(5) + SignedQuantity(5)  # type: ignore[operator]
