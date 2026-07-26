"""Dimensionless proportions.

One type, three ways of writing the same thing. GST at eighteen percent is
``Ratio.from_percent(18)``, ``Ratio.from_basis_points(1800)`` and
``Ratio.from_fraction(Decimal("0.18"))`` — all equal, all the same object.

Separate ``Percentage`` and ``BasisPoints`` types were considered and rejected.
They would be three types for one concept, and every function taking a rate would
have to decide which to accept. Named constructors and named accessors give the
readability without the fragmentation.

Unlike :class:`~dhruva.shared.money.money.Money`, :class:`Ratio` is
:class:`~decimal.Decimal`-backed. Rates are applied per trade rather than per
tick, so the exactness and readability of decimal fractions is worth more here
than the speed of integers, and a rate is a ratio of two exact quantities rather
than a count of anything.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Self

from dhruva.shared.invariants import invariant

__all__ = ["Ratio"]

_HUNDRED: Final = Decimal(100)
_TEN_THOUSAND: Final = Decimal(10_000)


class Ratio:
    """A dimensionless proportion, exact and immutable.

    Examples
    --------
    >>> gst = Ratio.from_percent(18)
    >>> gst.as_percent()
    Decimal('18')
    >>> gst.as_basis_points()
    Decimal('1800')
    >>> str(gst)
    '18%'
    >>> Ratio.from_basis_points(1800) == gst
    True
    """

    __slots__ = ("_fraction",)

    _fraction: Decimal

    def __init__(self, fraction: Decimal, /) -> None:
        """Wrap an exact decimal fraction.

        Prefer the named constructors; they say which unit the caller is thinking
        in, which is the whole reason this type exists.

        Raises
        ------
        InvariantViolation
            If ``fraction`` is a ``float``, or is not finite.
        """
        invariant(
            not isinstance(fraction, float),
            "Ratio cannot be built from a float; use Decimal or a named constructor",
            value=repr(fraction),
        )
        invariant(fraction.is_finite(), "Ratio must be finite", value=str(fraction))
        object.__setattr__(self, "_fraction", fraction)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; value objects are frozen."""
        msg = f"Ratio is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def from_fraction(cls, fraction: Decimal | int | str, /) -> Self:
        """Build from a fraction, where ``0.18`` means eighteen percent."""
        return cls(_to_decimal(fraction))

    @classmethod
    def from_percent(cls, percent: Decimal | int | str, /) -> Self:
        """Build from a percentage, where ``18`` means eighteen percent."""
        return cls(_to_decimal(percent) / _HUNDRED)

    @classmethod
    def from_basis_points(cls, basis_points: Decimal | int | str, /) -> Self:
        """Build from basis points, where ``1800`` means eighteen percent.

        One basis point is one hundredth of a percent. Brokerage and impact-cost
        figures are conventionally quoted this way.
        """
        return cls(_to_decimal(basis_points) / _TEN_THOUSAND)

    @classmethod
    def zero(cls) -> Self:
        """Return the zero ratio."""
        return cls(Decimal(0))

    # -- accessors ---------------------------------------------------------- #

    @property
    def fraction(self) -> Decimal:
        """Return the underlying fraction, where ``0.18`` means eighteen percent."""
        return self._fraction

    def as_percent(self) -> Decimal:
        """Return the value in percent, where eighteen percent gives ``18``."""
        return self._fraction * _HUNDRED

    def as_basis_points(self) -> Decimal:
        """Return the value in basis points, where eighteen percent gives ``1800``."""
        return self._fraction * _TEN_THOUSAND

    @property
    def is_zero(self) -> bool:
        """Return whether this ratio is exactly zero."""
        return self._fraction == 0

    # -- arithmetic --------------------------------------------------------- #

    def __add__(self, other: Ratio, /) -> Ratio:
        """Add two ratios, as when combining CGST and SGST components."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return Ratio(self._fraction + other._fraction)

    def __sub__(self, other: Ratio, /) -> Ratio:
        """Subtract one ratio from another."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return Ratio(self._fraction - other._fraction)

    def __mul__(self, other: int, /) -> Ratio:
        """Scale a ratio by a whole number."""
        if not isinstance(other, int) or isinstance(other, bool):
            return NotImplemented
        return Ratio(self._fraction * other)

    def __neg__(self) -> Ratio:
        """Return the additive inverse."""
        return Ratio(-self._fraction)

    # -- comparison and identity -------------------------------------------- #

    def __eq__(self, other: object, /) -> bool:
        """Compare by value. ``18%`` equals ``1800bps`` equals ``0.18``."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return self._fraction == other._fraction

    def __lt__(self, other: Ratio, /) -> bool:
        """Order by fraction."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return self._fraction < other._fraction

    def __le__(self, other: Ratio, /) -> bool:
        """Order by fraction."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return self._fraction <= other._fraction

    def __gt__(self, other: Ratio, /) -> bool:
        """Order by fraction."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return self._fraction > other._fraction

    def __ge__(self, other: Ratio, /) -> bool:
        """Order by fraction."""
        if not isinstance(other, Ratio):
            return NotImplemented
        return self._fraction >= other._fraction

    def __bool__(self) -> bool:
        """Return whether this ratio is non-zero.

        Present so that ``if rate:`` reads the same way it does for every other
        value object here. Without it, ``Ratio.zero()`` would be truthy while
        ``Money.zero()`` is falsy -- an inconsistency that would eventually be
        discovered by a conditional that silently took the wrong branch.
        """
        return self._fraction != 0

    def __hash__(self) -> int:
        """Hash by normalised value, so equal ratios hash equally.

        ``Decimal("0.18")`` and ``Decimal("0.180")`` are equal but have different
        representations; normalising keeps the hash/equality contract intact.
        """
        return hash(self._fraction.normalize())

    def __str__(self) -> str:
        """Render as a percentage, which is how rates are usually discussed."""
        return f"{self.as_percent().normalize():f}%"

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return f"Ratio.from_fraction('{self._fraction}')"


def _to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert an accepted input to Decimal, refusing floats.

    Raises
    ------
    InvariantViolation
        If ``value`` is a ``float``. A float rate is how an eighteen percent GST
        becomes 17.999999999999996 percent.
    """
    invariant(
        not isinstance(value, float),
        "Ratio cannot be built from a float",
        value=repr(value),
    )
    return value if isinstance(value, Decimal) else Decimal(value)
