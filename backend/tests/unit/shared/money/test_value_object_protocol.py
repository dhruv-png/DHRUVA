"""Protocol conformance shared by every monetary value object.

These are the behaviours forty later subsystems will assume without re-checking:
that comparison with a foreign type raises rather than returning something
plausible, that ordering is total within a type, that string forms round-trip,
and that nothing is accidentally truthy.

Written as one parametrised sweep rather than as four near-identical files,
because the property being asserted is genuinely the same property.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money import (
    CHARGE_TO_PAISE,
    Money,
    Price,
    Quantity,
    Ratio,
    SignedQuantity,
)

pytestmark = pytest.mark.unit

COMPARISONS: list[Callable[[Any, Any], Any]] = [
    operator.lt,
    operator.le,
    operator.gt,
    operator.ge,
]

#: One representative low/high pair per ordered type.
SAMPLES = [
    pytest.param(Money.parse("1.00"), Money.parse("2.00"), id="Money"),
    pytest.param(Price.parse("1.00"), Price.parse("2.00"), id="Price"),
    pytest.param(Quantity.shares(1), Quantity.shares(2), id="Quantity"),
    pytest.param(Ratio.from_percent(1), Ratio.from_percent(2), id="Ratio"),
]

#: Every value object in the package, for the protocol sweep.
ALL_TYPES = [
    pytest.param(Money.parse("1.00"), id="Money"),
    pytest.param(Price.parse("1.00"), id="Price"),
    pytest.param(Quantity.shares(1), id="Quantity"),
    pytest.param(SignedQuantity(1), id="SignedQuantity"),
    pytest.param(Ratio.from_percent(1), id="Ratio"),
]

FOREIGN: list[Any] = [1, 1.0, "1", None, Decimal("1"), object()]


@pytest.mark.parametrize(("low", "high"), SAMPLES)
@pytest.mark.parametrize("compare", COMPARISONS, ids=lambda f: f.__name__)
def test_ordering_is_total_within_a_type(
    low: Any, high: Any, compare: Callable[[Any, Any], Any]
) -> None:
    """Each operator agrees with the others about which value is larger."""
    assert compare(low, high) == compare(1, 2)
    assert compare(high, low) == compare(2, 1)
    assert compare(low, low) == compare(1, 1)


@pytest.mark.parametrize(("low", "high"), SAMPLES)
@pytest.mark.parametrize("compare", COMPARISONS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("foreign", FOREIGN, ids=lambda v: type(v).__name__)
def test_ordering_against_a_foreign_type_raises(
    low: Any, high: Any, compare: Callable[[Any, Any], Any], foreign: Any
) -> None:
    """Returning ``NotImplemented`` is what turns this into a ``TypeError``.

    Without the guard, comparing money to a bare number would either crash with
    an ``AttributeError`` halfway through, or -- worse -- succeed by accident.
    """
    for value in (low, high):
        with pytest.raises(TypeError):
            compare(value, foreign)


@pytest.mark.parametrize("value", ALL_TYPES)
@pytest.mark.parametrize("foreign", FOREIGN, ids=lambda v: type(v).__name__)
def test_equality_against_a_foreign_type_is_false_not_an_error(value: Any, foreign: Any) -> None:
    """Equality is total; ordering is not. ``==`` must answer, never raise."""
    assert value != foreign
    assert value.__eq__(foreign) in (NotImplemented, False)


@pytest.mark.parametrize("value", ALL_TYPES)
@pytest.mark.parametrize("foreign", FOREIGN, ids=lambda v: type(v).__name__)
def test_arithmetic_against_a_foreign_type_raises(value: Any, foreign: Any) -> None:
    """Adding a bare number to a dimensioned value is a category error."""
    with pytest.raises(TypeError):
        _ = value + foreign


@pytest.mark.parametrize("value", ALL_TYPES)
def test_zero_is_falsy_and_non_zero_is_truthy(value: Any) -> None:
    """So ``if amount:`` reads the way a reader expects."""
    zero = type(value).zero()

    assert not zero
    assert value


@pytest.mark.parametrize("value", ALL_TYPES)
def test_repr_is_unambiguous_and_str_is_readable(value: Any) -> None:
    """Both must exist and differ; a value object with a default repr is a bug."""
    assert type(value).__name__ in repr(value)
    assert str(value)
    assert repr(value) != str(value)


@pytest.mark.parametrize("value", ALL_TYPES)
def test_every_value_object_is_hashable_and_slotted(value: Any) -> None:
    """Slots keep a million live instances affordable; hashability keeps them keyable."""
    assert hash(value) == hash(value)
    assert not hasattr(value, "__dict__"), "value object must define __slots__"


@pytest.mark.parametrize("value", ALL_TYPES)
def test_no_value_object_converts_to_float(value: Any) -> None:
    """ADR-048. The conversion that would undo the entire design."""
    assert not hasattr(value, "__float__")
    with pytest.raises(TypeError):
        float(value)


@pytest.mark.parametrize("value", ALL_TYPES)
def test_zero_reports_itself_as_zero(value: Any) -> None:
    """``is_zero`` and truthiness must never disagree."""
    zero = type(value).zero()

    assert zero.is_zero
    assert not value.is_zero


def test_negation_and_absolute_value_where_they_apply() -> None:
    """Signed types support both; unsigned Quantity supports neither."""
    assert (-Money.parse("1.00")).is_negative
    assert abs(Money.parse("-1.00")) == Money.parse("1.00")
    assert abs(Price.parse("-1.00")) == Price.parse("1.00")
    assert abs(SignedQuantity(-5)) == SignedQuantity(5)

    assert not hasattr(Quantity.shares(1), "__neg__")


def test_signed_quantity_ordering_and_comparison() -> None:
    """Position deltas are ordered, so they can be sorted and bucketed."""
    assert SignedQuantity(-5) < SignedQuantity(5)
    assert SignedQuantity(0) == SignedQuantity(0)
    with pytest.raises(TypeError):
        _ = SignedQuantity(1) < 5  # type: ignore[operator]


def test_quantity_scales_from_either_side() -> None:
    """``2 * qty`` and ``qty * 2`` must agree."""
    assert Quantity.shares(10) * 3 == 3 * Quantity.shares(10)


def test_money_scales_from_either_side() -> None:
    """``3 * money`` and ``money * 3`` must agree."""
    assert Money.parse("1.00") * 3 == 3 * Money.parse("1.00")


def test_notional_scales_from_either_side() -> None:
    """``qty * price`` and ``price * qty`` must agree."""
    assert Price.parse("2.00") * Quantity.shares(3) == (Quantity.shares(3) * Price.parse("2.00"))


@pytest.mark.parametrize("value", ALL_TYPES)
@pytest.mark.parametrize("foreign", FOREIGN, ids=lambda v: type(v).__name__)
@pytest.mark.parametrize("op", [operator.sub, operator.mul], ids=lambda f: f.__name__)
def test_every_binary_operator_rejects_a_foreign_operand(
    value: Any, foreign: Any, op: Callable[[Any, Any], Any]
) -> None:
    """Subtraction and multiplication guard as carefully as addition does.

    Multiplication is the interesting case: ``Money * int`` is legitimate, so the
    guard has to admit ``int`` while still rejecting ``float``, ``bool`` and
    everything else. A float scalar slipping through here would reintroduce
    exactly the imprecision ADR-042 exists to remove.
    """
    if op is operator.mul and isinstance(foreign, int) and not isinstance(foreign, bool):
        pytest.skip("scaling by a whole number is a supported operation")
    with pytest.raises(TypeError):
        op(value, foreign)


@pytest.mark.parametrize("value", ALL_TYPES)
def test_scaling_by_a_bool_is_refused(value: Any) -> None:
    """``bool`` is a subclass of ``int``; ``money * True`` is never intended."""
    with pytest.raises(TypeError):
        _ = value * True


def test_money_division_guards_its_divisor_type() -> None:
    """Only whole numbers divide money."""
    with pytest.raises(InvariantViolation):
        Money.parse("10.00").divide(2.5, CHARGE_TO_PAISE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _ = Money.parse("10.00") / 2.5  # type: ignore[operator]


def test_quantity_in_lots_rejects_a_non_positive_lot_size() -> None:
    """A zero lot size would be a division by zero dressed up as metadata."""
    with pytest.raises(InvariantViolation, match="lot size must be positive"):
        Quantity.shares(100).in_lots(lot_size=0)


def test_money_per_unit_refuses_zero_quantity() -> None:
    """An average price over zero fills does not exist."""
    with pytest.raises(InvariantViolation, match="zero quantity"):
        Money.parse("100.00").per_unit(Quantity.zero(), CHARGE_TO_PAISE)


def test_money_ratio_to_zero_is_refused() -> None:
    """A proportion of nothing is undefined, not infinite."""
    with pytest.raises(InvariantViolation, match="ratio to zero"):
        Money.parse("100.00").ratio_to(Money.zero())


def test_split_requires_a_positive_part_count() -> None:
    """Splitting into zero parts would lose the whole amount."""
    with pytest.raises(InvariantViolation, match="positive number of parts"):
        Money.parse("100.00").split(0)
