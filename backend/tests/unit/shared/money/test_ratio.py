"""Ratio: one type, three ways of writing a rate."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money import Ratio
from tests.unit.shared.money import strategies as gen

pytestmark = pytest.mark.unit


def test_the_three_constructors_agree() -> None:
    """Eighteen percent is 1800 basis points is 0.18. One type, three spellings.

    Separate Percentage and BasisPoints types were rejected: they would be three
    types for one concept, and every function taking a rate would have to decide
    which to accept.
    """
    assert Ratio.from_percent(18) == Ratio.from_basis_points(1800)
    assert Ratio.from_percent(18) == Ratio.from_fraction(Decimal("0.18"))


@given(percent=st.integers(-10_000, 10_000))
def test_percent_round_trip(percent: int) -> None:
    """Constructing from percent and reading it back is lossless."""
    assert Ratio.from_percent(percent).as_percent() == Decimal(percent)


@given(bps=st.integers(-1_000_000, 1_000_000))
def test_basis_point_round_trip(bps: int) -> None:
    """Constructing from basis points and reading back is lossless."""
    assert Ratio.from_basis_points(bps).as_basis_points() == Decimal(bps)


@pytest.mark.parametrize("bad", [0.18, 1.0, float("nan"), float("inf")])
def test_float_rates_are_refused(bad: float) -> None:
    """A float rate is how eighteen percent becomes 17.999999999999996 percent."""
    with pytest.raises(InvariantViolation):
        Ratio.from_fraction(bad)  # type: ignore[arg-type]


@given(a=gen.ratio(), b=gen.ratio())
def test_addition_is_commutative(a: Ratio, b: Ratio) -> None:
    """As when combining CGST and SGST components."""
    assert a + b == b + a


def test_gst_components_sum_to_the_whole() -> None:
    """CGST 9% plus SGST 9% is IGST 18%. A figure a reviewer can check."""
    assert Ratio.from_percent(9) + Ratio.from_percent(9) == Ratio.from_percent(18)


@given(a=gen.ratio())
def test_negation_is_an_involution(a: Ratio) -> None:
    """Negating twice returns the original."""
    negated = -a

    assert -negated == a


@given(fraction=st.decimals(min_value=-100, max_value=100, places=6, allow_nan=False))
def test_equal_ratios_hash_equally_despite_representation(fraction: Decimal) -> None:
    """``Decimal('0.18')`` and ``Decimal('0.180')`` are equal and must hash alike.

    Without normalising in ``__hash__`` this contract breaks silently, and dict
    lookups start missing for reasons nobody can reproduce.
    """
    padded = Decimal(str(fraction) + "0")

    assert Ratio(fraction) == Ratio(padded)
    assert hash(Ratio(fraction)) == hash(Ratio(padded))


@given(a=gen.ratio(), b=gen.ratio())
def test_ordering_is_consistent_with_equality(a: Ratio, b: Ratio) -> None:
    """Trichotomy."""
    assert sum([a < b, a == b, a > b]) == 1


@given(fraction=st.decimals(min_value=-10, max_value=10, places=4, allow_nan=False))
def test_ratio_is_immutable(fraction: Decimal) -> None:
    """A value object that can be mutated is not a value object."""
    with pytest.raises(AttributeError):
        Ratio(fraction)._fraction = Decimal(0)  # asserting immutability


def test_string_form_reads_as_a_percentage() -> None:
    """Rates are discussed in percent, so that is how they render."""
    assert str(Ratio.from_percent(18)) == "18%"
    assert str(Ratio.from_basis_points(25)) == "0.25%"


def test_zero_ratio() -> None:
    """The additive identity, and the no-charge case."""
    assert Ratio.zero().is_zero
    assert Ratio.from_percent(5) + Ratio.zero() == Ratio.from_percent(5)
