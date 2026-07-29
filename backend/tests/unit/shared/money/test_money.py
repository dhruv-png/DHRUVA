"""Money: exactness, algebraic laws, and the invariants that protect capital.

These are the tests every later subsystem trusts without re-checking. If Money is
wrong, the cost engine is wrong, the P&L is wrong, and every backtest is wrong --
and none of them will crash.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.money import (
    CHARGE_TO_PAISE,
    CONSERVATIVE_TO_TRADER,
    STATISTICAL,
    STT_NEAREST_RUPEE,
    Currency,
    Money,
    Quantity,
    Ratio,
)
from tests.unit.shared.money import strategies as gen

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


@given(units=gen.minor_units())
def test_minor_units_round_trip(units: int) -> None:
    """The storage representation is the identity. No conversion can be wrong."""
    assert Money.from_minor_units(units).minor_units == units


@given(major=st.integers(min_value=0, max_value=10**9), minor=st.integers(0, 99))
def test_of_composes_major_and_minor(major: int, minor: int) -> None:
    """``Money.of(1234, 56)`` means ₹1234.56."""
    assert Money.of(major, minor).minor_units == major * 100 + minor


@given(major=st.integers(min_value=1, max_value=10**9), minor=st.integers(0, 99))
def test_of_applies_the_sign_to_the_whole_amount(major: int, minor: int) -> None:
    """``Money.of(-1234, 56)`` is -1234.56, not -1233.44.

    The minor part is a magnitude, never a signed offset. Getting this wrong
    would misprice every negative amount by up to one rupee.
    """
    assert Money.of(-major, minor) == -Money.of(major, minor)


@given(units=gen.minor_units())
def test_repr_round_trips_through_parse(units: int) -> None:
    """A value object must be reconstructable from its own representation."""
    original = Money(units)
    text = repr(original).split("'")[1]
    assert Money.parse(text) == original


@pytest.mark.parametrize(
    ("text", "expected_minor"),
    [
        ("0", 0),
        ("0.00", 0),
        ("1", 100),
        ("1.5", 150),
        ("1.05", 105),
        ("-1.05", -105),
        ("+1.05", 105),
        ("  12.34  ", 1234),
        ("1234.56", 123456),
    ],
)
def test_parse_accepts_valid_forms(text: str, expected_minor: int) -> None:
    """Parsing covers the shapes that arrive from brokers and files."""
    assert Money.parse(text).minor_units == expected_minor


@pytest.mark.parametrize("text", ["", "abc", "1.2.3", "1,234.56", "--1", "1e5", " ", ".5", "1."])
def test_parse_rejects_malformed_input(text: str) -> None:
    """Malformed money must never become a plausible number."""
    with pytest.raises(InvariantViolation, match="malformed"):
        Money.parse(text)


def test_parse_rejects_excess_precision() -> None:
    """Excess precision raises rather than rounding.

    A value arriving with more precision than the currency supports means the
    caller believes something about it that is not true. Silently discarding the
    extra digits would confirm the belief.
    """
    with pytest.raises(InvariantViolation, match="decimal places"):
        Money.parse("1.234")


@pytest.mark.parametrize("bad", [1.0, 1.5, True, None, Decimal("1"), "100"])
def test_construction_rejects_anything_but_int(bad: object) -> None:
    """Especially float. This is the single most consequential rejection here."""
    with pytest.raises(InvariantViolation):
        Money(bad)  # type: ignore[arg-type]  # asserting runtime rejection


def test_of_rejects_an_out_of_range_minor_part() -> None:
    """``Money.of(1, 150)`` is a typo, not one rupee fifty."""
    with pytest.raises(InvariantViolation, match="out of range"):
        Money.of(1, 150)


# --------------------------------------------------------------------------- #
# Algebraic laws
# --------------------------------------------------------------------------- #


@given(a=gen.money(), b=gen.money())
def test_addition_is_commutative(a: Money, b: Money) -> None:
    """A property, not three examples."""
    assert a + b == b + a


@given(a=gen.money(), b=gen.money(), c=gen.money())
def test_addition_is_associative(a: Money, b: Money, c: Money) -> None:
    """Exactness means grouping cannot change the result. Float would fail this."""
    assert (a + b) + c == a + (b + c)


@given(a=gen.money())
def test_zero_is_the_additive_identity(a: Money) -> None:
    """Adding nothing changes nothing."""
    assert a + Money.zero() == a


@given(a=gen.money())
def test_negation_is_an_involution(a: Money) -> None:
    """Negating twice returns the original."""
    negated = -a

    assert -negated == a


@given(a=gen.money(), b=gen.money())
def test_subtraction_is_addition_of_the_negation(a: Money, b: Money) -> None:
    """The two paths must agree exactly."""
    assert a - b == a + (-b)


@given(a=gen.money(), n=st.integers(-1000, 1000), m=st.integers(-1000, 1000))
def test_multiplication_distributes_over_addition(a: Money, n: int, m: int) -> None:
    """Distributivity holds exactly, with no accumulated error."""
    assert a * (n + m) == a * n + a * m


@given(a=gen.money())
def test_multiplication_by_one_is_the_identity(a: Money) -> None:
    """Scaling by one changes nothing."""
    assert a * 1 == a


@given(a=gen.money())
def test_absolute_value_is_never_negative(a: Money) -> None:
    """The magnitude of any amount is non-negative."""
    assert not abs(a).is_negative


# --------------------------------------------------------------------------- #
# Allocation -- the property that protects reconciliation
# --------------------------------------------------------------------------- #


@given(amount=gen.money(), weights=gen.weights())
def test_allocation_never_loses_or_creates_a_paisa(amount: Money, weights: list[int]) -> None:
    """The defining property. Splitting ₹100 three ways must still total ₹100.

    Vanishing minor units accumulate across brokerage apportionment, STT
    apportionment and per-lot P&L into reconciliation failures that are very
    unpleasant to trace.
    """
    parts = amount.allocate(weights)

    assert sum(part.minor_units for part in parts) == amount.minor_units
    assert len(parts) == len(weights)


@given(amount=gen.money(), weights=gen.weights())
def test_allocation_is_deterministic(amount: Money, weights: list[int]) -> None:
    """Same inputs, same split, every time (ADR-011)."""
    assert amount.allocate(weights) == amount.allocate(weights)


@given(amount=gen.positive_money(), weights=gen.weights())
def test_a_strictly_larger_weight_never_receives_a_smaller_share(
    amount: Money, weights: list[int]
) -> None:
    """Monotonicity holds for *strictly* different weights.

    Equal weights are deliberately excluded. Splitting one paisa two ways must
    produce [1, 0]: some pair has to differ, or the parts would not sum to the
    whole. Requiring equal weights to receive equal shares would be requiring
    allocation to lose money, which is the one thing it must never do.
    """
    parts = amount.allocate(weights)

    for weight_a, part_a in zip(weights, parts, strict=True):
        for weight_b, part_b in zip(weights, parts, strict=True):
            if weight_a > weight_b:
                assert part_a >= part_b


@given(amount=gen.money(), parts=st.integers(1, 20))
def test_split_is_lossless(amount: Money, parts: int) -> None:
    """Equal division is allocation with equal weights."""
    assert sum(p.minor_units for p in amount.split(parts)) == amount.minor_units


def test_the_classic_hundred_split_three_ways() -> None:
    """The example everyone knows, pinned so a regression is obvious."""
    assert Money.parse("100.00").allocate([1, 1, 1]) == [
        Money.parse("33.34"),
        Money.parse("33.33"),
        Money.parse("33.33"),
    ]


@pytest.mark.parametrize("bad", [[], [0, 0], [-1, 2]])
def test_allocation_rejects_invalid_weights(bad: list[int]) -> None:
    """Empty, all-zero and negative weight vectors are all programming errors."""
    with pytest.raises(InvariantViolation):
        Money.parse("100.00").allocate(bad)


# --------------------------------------------------------------------------- #
# Rates and rounding
# --------------------------------------------------------------------------- #


@given(amount=gen.money())
def test_applying_a_zero_rate_yields_zero(amount: Money) -> None:
    """No rate, no charge."""
    assert amount.apply(Ratio.zero(), CHARGE_TO_PAISE).is_zero


@given(amount=gen.money())
def test_applying_a_unit_rate_is_the_identity(amount: Money) -> None:
    """One hundred percent of an amount is the amount."""
    assert amount.apply(Ratio.from_percent(100), CHARGE_TO_PAISE) == amount


@given(amount=gen.positive_money(), rate=gen.positive_ratio())
def test_conservative_rounding_never_understates_a_charge(amount: Money, rate: Ratio) -> None:
    """Optimistic cost estimates are how a strategy appears profitable and is not.

    Takes ``positive_ratio()`` rather than ``ratio()`` with an ``assume``. The
    filtered version rejected roughly seven inputs in eight and failed the
    ``filter_too_much`` health check on some seeds and not others, which made
    this test's result a property of the random seed rather than of the code.
    """
    conservative = amount.apply(rate, CONSERVATIVE_TO_TRADER)
    statistical = amount.apply(rate, STATISTICAL)

    assert conservative >= statistical


def test_a_negative_rate_produces_a_credit() -> None:
    """Rebates and credits are negative rates, not a separate operation."""
    rebate = Money.parse("1000.00").apply(Ratio.from_percent(-2), CHARGE_TO_PAISE)

    assert rebate == Money.parse("-20.00")
    assert rebate.is_negative


@given(amount=gen.money(), rate=gen.ratio())
def test_applying_a_rate_and_its_negation_are_opposites(amount: Money, rate: Ratio) -> None:
    """Sign handling in the rate path must be symmetric.

    Asserted to within one minor unit, because each direction rounds
    independently and half-up is not symmetric about zero.
    """
    positive = amount.apply(rate, STATISTICAL)
    negative = amount.apply(-rate, STATISTICAL)

    assert abs(positive.minor_units + negative.minor_units) <= 1


def test_gst_at_eighteen_percent() -> None:
    """A figure a reviewer can check against the rule."""
    assert Money.parse("1000.00").apply(Ratio.from_percent(18), CHARGE_TO_PAISE) == (
        Money.parse("180.00")
    )


def test_stt_rounds_to_the_nearest_rupee() -> None:
    """STT is charged in whole rupees; 3.0864 becomes 3."""
    charge = Money.parse("1234.56").apply(Ratio.from_basis_points(25), STT_NEAREST_RUPEE)

    assert charge == Money.parse("3.00")
    assert charge.minor_units % 100 == 0


def test_half_even_and_half_up_differ_exactly_at_the_midpoint() -> None:
    """Both are correct; which is correct *here* is the caller's decision."""
    amount = Money.parse("1.00")
    half = Ratio.from_fraction(Decimal("0.005"))

    assert amount.apply(half, STATISTICAL) == Money.parse("0.00")
    assert amount.apply(half, CHARGE_TO_PAISE) == Money.parse("0.01")


@given(amount=gen.money(), divisor=st.integers(-1000, 1000))
def test_division_requires_an_explicit_policy(amount: Money, divisor: int) -> None:
    """Bare division refuses to round, so nothing rounds by accident (ADR-044)."""
    assume(divisor != 0)
    exact = amount.minor_units % divisor == 0

    if exact:
        assert (amount / divisor).minor_units == amount.minor_units // divisor
    else:
        with pytest.raises(InvariantViolation, match="not exact"):
            _ = amount / divisor


def test_division_by_zero_is_refused() -> None:
    """Not a rounding question."""
    with pytest.raises(InvariantViolation, match="divide money by zero"):
        Money.parse("100.00").divide(0, CHARGE_TO_PAISE)


# --------------------------------------------------------------------------- #
# Dimensional typing (ADR-043)
# --------------------------------------------------------------------------- #


@given(amount=gen.money(), quantity=gen.positive_quantity())
def test_money_divided_by_quantity_is_a_price(amount: Money, quantity: Quantity) -> None:
    """Consideration over units is a unit price, and the type says so."""
    unit_price = amount.per_unit(quantity, CHARGE_TO_PAISE)

    assert unit_price.currency is amount.currency


def test_money_divided_by_money_is_dimensionless() -> None:
    """A proportion of an amount is a Ratio, not an amount."""
    result = Money.parse("50.00").ratio_to(Money.parse("200.00"))

    assert isinstance(result, Ratio)
    assert result == Ratio.from_percent(25)


def test_money_times_money_does_not_exist() -> None:
    """Rupees squared is not a quantity this platform has any use for."""
    with pytest.raises(TypeError):
        # mypy also rejects this statically; the ignore proves it does.
        _ = Money.of(1) * Money.of(2)  # type: ignore[operator]


def test_money_plus_int_does_not_exist() -> None:
    """A bare number has no currency, so it cannot be added to one that does."""
    with pytest.raises(TypeError):
        _ = Money.of(1) + 5  # type: ignore[operator]  # asserting refusal


# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #


def test_equality_incorporates_currency_not_just_amount() -> None:
    """Equality and hashing both include the currency.

    Only INR exists today, so cross-currency inequality cannot be exercised
    directly. What *can* be asserted is that currency participates in identity at
    all -- verified through the hash, which is where a forgotten field silently
    breaks dict lookups. This test becomes stronger the day a second currency is
    added, and it will already be here.
    """
    amount = Money(100, Currency.INR)

    assert hash(amount) == hash(("Money", 100, Currency.INR))
    assert amount != 100
    assert amount != "₹1.00"


@given(a=gen.money(), b=gen.money())
def test_same_currency_arithmetic_always_succeeds(a: Money, b: Money) -> None:
    """The runtime guard must not fire on the ordinary path (Design Review Q2)."""
    assert (a + b).currency is Currency.INR


# --------------------------------------------------------------------------- #
# Value-object semantics
# --------------------------------------------------------------------------- #


@given(units=gen.minor_units())
def test_equal_amounts_hash_equally(units: int) -> None:
    """The hash/equality contract, without which dict lookups silently fail."""
    assert hash(Money(units)) == hash(Money(units))
    assert Money(units) == Money(units)


@given(a=gen.money(), b=gen.money())
def test_ordering_is_consistent_with_equality(a: Money, b: Money) -> None:
    """Trichotomy: exactly one of <, ==, > holds."""
    assert sum([a < b, a == b, a > b]) == 1


@given(units=gen.minor_units())
def test_money_is_immutable(units: int) -> None:
    """A value object that can be mutated is not a value object."""
    amount = Money(units)

    with pytest.raises(AttributeError):
        amount._minor_units = 999  # asserting immutability


@given(units=gen.minor_units())
def test_money_is_usable_as_a_dict_key(units: int) -> None:
    """Used throughout the platform for grouping and aggregation."""
    assert {Money(units): "value"}[Money(units)] == "value"


@given(units=gen.minor_units())
def test_as_decimal_is_exact(units: int) -> None:
    """Display conversion must not distort the value it displays."""
    assert Money(units).as_decimal() == Decimal(units).scaleb(-2)


def test_money_has_no_float_conversion() -> None:
    """``float(money)`` must not exist. It is the whole failure mode (ADR-048)."""
    assert not hasattr(Money.of(1), "__float__")


# --------------------------------------------------------------------------- #
# Boundary conditions
# --------------------------------------------------------------------------- #


def test_arithmetic_is_exact_at_the_bigint_boundary() -> None:
    """Python ints do not overflow; the ceiling belongs to the storage layer (S04).

    Asserted so that the day a column overflows, it is understood as a
    persistence limit rather than a Money defect.
    """
    ceiling = Money(gen.MAX_MINOR_UNITS)

    assert (ceiling + Money(1)).minor_units == gen.MAX_MINOR_UNITS + 1


@given(units=st.integers(min_value=-(10**30), max_value=10**30))
def test_arbitrarily_large_amounts_stay_exact(units: int) -> None:
    """Far beyond any real amount, the arithmetic still cannot drift."""
    assert (Money(units) * 3 - Money(units) * 2) == Money(units)
