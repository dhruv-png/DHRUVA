"""One decimal value has exactly one canonical text, whatever its scale.

The defect this closes: a close written as ``Decimal("100")`` comes back from a
``NUMERIC(_, 8)`` column as ``Decimal("100.00000000")``, and ``str`` renders the
two differently. Anything serialising with ``str`` therefore produced output
that depended on whether the value had been through the database — which, in a
snapshot whose whole claim is determinism, is a defect rather than a cosmetic
difference.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dhruva.shared.decimals import canonical_decimal, canonical_decimal_or_none

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("100.00000000", "100"),
        ("100", "100"),
        ("100.0", "100"),
        ("1E+2", "100"),
        ("143.50", "143.5"),
        ("0.9300", "0.93"),
        ("-10.00", "-10"),
        ("-0.010", "-0.01"),
        ("0.005", "0.005"),
        ("12345678.90", "12345678.9"),
    ],
)
def test_the_storage_scale_does_not_change_the_text(stored: str, expected: str) -> None:
    """The value is canonical, not the column it happened to be read from."""
    assert canonical_decimal(Decimal(stored)) == expected


@pytest.mark.parametrize("written", ["100", "100.00", "100.00000000", "1E+2"])
def test_every_spelling_of_one_value_renders_identically(written: str) -> None:
    """The property the snapshot's determinism actually rests on."""
    assert canonical_decimal(Decimal(written)) == canonical_decimal(Decimal(100))


@pytest.mark.parametrize("zero", ["0", "0.00", "-0", "-0.00", "0E-8"])
def test_every_zero_renders_as_a_plain_zero(zero: str) -> None:
    """A signed zero is an artefact of arithmetic, not a number anybody wrote."""
    assert canonical_decimal(Decimal(zero)) == "0"


def test_a_large_value_never_becomes_scientific_notation() -> None:
    """``normalize`` alone produces ``1E+9``, which no consumer expects to parse."""
    assert canonical_decimal(Decimal("1000000000.00")) == "1000000000"
    assert "E" not in canonical_decimal(Decimal("1E+18"))


def test_a_small_value_never_becomes_scientific_notation() -> None:
    """The same hazard at the other end of the scale."""
    assert "E" not in canonical_decimal(Decimal("1E-8"))
    assert canonical_decimal(Decimal("0.00000001")) == "0.00000001"


def test_the_text_parses_back_to_the_same_value() -> None:
    """Canonicalising is a change of spelling, never a change of number."""
    for written in ("100.00000000", "143.50", "-0.010", "0.00000001", "1E+18"):
        assert Decimal(canonical_decimal(Decimal(written))) == Decimal(written)


def test_absence_stays_absence() -> None:
    """A missing close must not become "0", which reads as a price."""
    assert canonical_decimal_or_none(None) is None
    assert canonical_decimal_or_none(Decimal("100.00")) == "100"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_decimal_is_refused(bad: str) -> None:
    """It has no exact text, and a file claiming exactness must not carry one."""
    with pytest.raises(ValueError, match="non-finite"):
        canonical_decimal(Decimal(bad))


def test_nothing_passes_through_a_binary_float() -> None:
    """The value that exposes it: 0.1 + 0.2 is exact in Decimal and not in float."""
    total = Decimal("0.1") + Decimal("0.2")

    assert canonical_decimal(total) == "0.3"
    assert canonical_decimal(Decimal("143.49999999999997")) == "143.49999999999997"
