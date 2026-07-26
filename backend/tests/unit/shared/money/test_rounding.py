"""Rounding policies: every mode, every sign, every boundary (ADR-044)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money.rounding import (
    CHARGE_TO_PAISE,
    CONSERVATIVE_TO_TRADER,
    EXACT,
    STATISTICAL,
    STT_NEAREST_RUPEE,
    RoundingMode,
    RoundingPolicy,
)

pytestmark = pytest.mark.unit

ALL_MODES = [m for m in RoundingMode if m is not RoundingMode.EXACT]


def _policy(mode: RoundingMode, quantum: int = 1) -> RoundingPolicy:
    return RoundingPolicy(f"TEST_{mode.value}", mode, quantum)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (RoundingMode.FLOOR, 1),
        (RoundingMode.CEILING, 2),
        (RoundingMode.DOWN, 1),
        (RoundingMode.UP, 2),
        (RoundingMode.HALF_UP, 2),
        (RoundingMode.HALF_EVEN, 2),
    ],
)
def test_positive_midpoint(mode: RoundingMode, expected: int) -> None:
    """3/2 = 1.5. Every mode pinned at the hardest case."""
    assert _policy(mode).apply(3, 2) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (RoundingMode.FLOOR, -2),
        (RoundingMode.CEILING, -1),
        (RoundingMode.DOWN, -1),
        (RoundingMode.UP, -2),
        (RoundingMode.HALF_UP, -2),
        (RoundingMode.HALF_EVEN, -2),
    ],
)
def test_negative_midpoint(mode: RoundingMode, expected: int) -> None:
    """-3/2 = -1.5. Sign handling is where rounding implementations go wrong."""
    assert _policy(mode).apply(-3, 2) == expected


@pytest.mark.parametrize(("numerator", "expected"), [(5, 2), (7, 4), (-5, -2), (-7, -4)])
def test_half_even_breaks_ties_to_the_even_neighbour(numerator: int, expected: int) -> None:
    """2.5 to 2, 3.5 to 4. Unbiased across many operations."""
    assert _policy(RoundingMode.HALF_EVEN).apply(numerator, 2) == expected


@given(
    numerator=st.integers(-(10**12), 10**12),
    denominator=st.integers(1, 10**6),
    mode=st.sampled_from(ALL_MODES),
)
def test_every_mode_lands_on_an_adjacent_integer(
    numerator: int, denominator: int, mode: RoundingMode
) -> None:
    """No mode may ever be more than one unit away from the true value."""
    result = _policy(mode).apply(numerator, denominator)
    floor, remainder = divmod(numerator, denominator)
    ceiling = floor if remainder == 0 else floor + 1

    assert floor <= result <= ceiling


@given(
    numerator=st.integers(-(10**9), 10**9),
    denominator=st.integers(1, 1000),
)
def test_exact_division_is_unaffected_by_mode(numerator: int, denominator: int) -> None:
    """When there is no remainder, every mode must agree."""
    exact = numerator * denominator
    results = {_policy(mode).apply(exact, denominator) for mode in ALL_MODES}

    assert results == {numerator}


@given(numerator=st.integers(-(10**9), 10**9), denominator=st.integers(1, 1000))
def test_up_and_down_bracket_the_true_value(numerator: int, denominator: int) -> None:
    """DOWN moves toward zero, UP away from it; the pair brackets the value."""
    down = _policy(RoundingMode.DOWN).apply(numerator, denominator)
    up = _policy(RoundingMode.UP).apply(numerator, denominator)

    assert abs(down) <= abs(up)


def test_exact_refuses_to_round() -> None:
    """Refusing to round is what makes bare operators safe (ADR-043 plus ADR-044)."""
    assert EXACT.apply(4, 2) == 2
    with pytest.raises(InvariantViolation, match="not exact"):
        EXACT.apply(3, 2)


@given(numerator=st.integers(0, 10**9))
def test_a_quantum_snaps_the_result_to_that_granularity(numerator: int) -> None:
    """STT rounds to whole rupees, which is a quantum of 100 paise."""
    result = STT_NEAREST_RUPEE.apply(numerator, 1)

    assert result % 100 == 0


def test_quantum_rounding_picks_the_nearest_multiple() -> None:
    """1250 paise is ₹12.50, which rounds to ₹13 under half-up."""
    assert STT_NEAREST_RUPEE.apply(1250, 1) == 1300
    assert STT_NEAREST_RUPEE.apply(1249, 1) == 1200


def test_a_non_positive_denominator_is_refused() -> None:
    """Callers normalise the sign before calling; this catches the ones that do not."""
    with pytest.raises(InvariantViolation, match="denominator must be positive"):
        CHARGE_TO_PAISE.apply(10, 0)


def test_a_non_positive_quantum_is_refused_at_construction() -> None:
    """A zero quantum could not round anything."""
    with pytest.raises(InvariantViolation, match="quantum"):
        RoundingPolicy("BAD", RoundingMode.HALF_UP, quantum=0)


def test_policies_are_named_after_the_market_rule() -> None:
    """The name is what a reviewer checks against a circular.

    ``STT_NEAREST_RUPEE`` is checkable against the Securities Transaction Tax
    rules; ``ROUND_HALF_UP`` is not.
    """
    assert str(STT_NEAREST_RUPEE) == "STT_NEAREST_RUPEE"
    assert str(CONSERVATIVE_TO_TRADER) == "CONSERVATIVE_TO_TRADER"
    assert str(STATISTICAL) == "STATISTICAL"


def test_policies_are_immutable_value_objects() -> None:
    """A policy that could be mutated at runtime would be no policy at all."""
    with pytest.raises((AttributeError, TypeError)):
        CHARGE_TO_PAISE.mode = RoundingMode.DOWN  # type: ignore[misc]  # asserting immutability
