"""Hypothesis strategies shared across the monetary property tests.

Bounds are chosen to exercise realistic magnitudes and the extremes either side
of them, without generating values so large that a failure tells you nothing.
``MAX_MINOR_UNITS`` is the ``BIGINT`` ceiling, because that is the real limit the
persistence layer will impose in S04.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from hypothesis import strategies as st

from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio, Side

#: Signed 64-bit ceiling. Amounts beyond this cannot be persisted (S04).
MAX_MINOR_UNITS: Final = 2**63 - 1

#: A realistic Indian-market range: roughly plus or minus ten billion rupees.
REALISTIC_MINOR_UNITS: Final = 10**12


def minor_units(*, max_magnitude: int = REALISTIC_MINOR_UNITS) -> st.SearchStrategy[int]:
    """Integer minor-unit counts, biased toward zero and the boundaries."""
    return st.integers(min_value=-max_magnitude, max_value=max_magnitude)


def money(*, max_magnitude: int = REALISTIC_MINOR_UNITS) -> st.SearchStrategy[Money]:
    """Arbitrary INR amounts."""
    return minor_units(max_magnitude=max_magnitude).map(lambda n: Money(n, Currency.INR))


def non_zero_money() -> st.SearchStrategy[Money]:
    """Arbitrary non-zero INR amounts, for division and ratio operations."""
    return money().filter(lambda m: not m.is_zero)


def positive_money() -> st.SearchStrategy[Money]:
    """Strictly positive INR amounts."""
    return st.integers(min_value=1, max_value=REALISTIC_MINOR_UNITS).map(
        lambda n: Money(n, Currency.INR)
    )


def price() -> st.SearchStrategy[Price]:
    """Arbitrary prices at the fixed eight-decimal scale."""
    return st.integers(min_value=-(10**16), max_value=10**16).map(Price)


def quantity(*, max_units: int = 10**7) -> st.SearchStrategy[Quantity]:
    """Non-negative quantities."""
    return st.integers(min_value=0, max_value=max_units).map(Quantity)


def positive_quantity(*, max_units: int = 10**7) -> st.SearchStrategy[Quantity]:
    """Strictly positive quantities."""
    return st.integers(min_value=1, max_value=max_units).map(Quantity)


def ratio() -> st.SearchStrategy[Ratio]:
    """Ratios spanning ordinary rate magnitudes, both signs."""
    return st.decimals(
        min_value=Decimal("-10"),
        max_value=Decimal("10"),
        places=8,
        allow_nan=False,
        allow_infinity=False,
    ).map(Ratio)


def weights(*, max_parts: int = 12) -> st.SearchStrategy[list[int]]:
    """Allocation weight vectors that are non-empty and sum to a positive total."""
    return st.lists(
        st.integers(min_value=0, max_value=1000), min_size=1, max_size=max_parts
    ).filter(lambda w: sum(w) > 0)


def side() -> st.SearchStrategy[Side]:
    """Either trade direction."""
    return st.sampled_from(list(Side))
