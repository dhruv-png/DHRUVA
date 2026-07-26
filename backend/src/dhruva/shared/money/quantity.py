"""Order and position sizes, and trade direction.

ADR-045: **quantity is always unsigned, and direction lives in
:class:`Side`.** Encoding "sell" as a negative number is a convention that works
until somebody forgets it, and the failure mode is a position with the wrong sign
— which reconciles cleanly against nothing and is discovered late.

Where a signed magnitude genuinely is the right model — a position delta, a net
change — :class:`SignedQuantity` exists and says so in its name.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from dhruva.shared.invariants import invariant

__all__ = ["Quantity", "Side", "SignedQuantity"]


class Side(StrEnum):
    """The direction of a trade.

    Attributes
    ----------
    BUY
        Increases a long position, or reduces a short one.
    SELL
        Increases a short position, or reduces a long one.
    """

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """Return ``+1`` for a buy and ``-1`` for a sell.

        The single sanctioned place where direction becomes a number. Every other
        conversion should go through here rather than reimplementing the mapping.
        """
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        """Return the side that closes a position opened on this one."""
        return Side.SELL if self is Side.BUY else Side.BUY


class Quantity:
    """A count of units. Always zero or positive.

    Units are absolute — shares, or contracts. Lot sizing is instrument metadata
    and lives in the Reference context (S07); :meth:`lots` requires it to be
    passed in explicitly, so the conversion is visible at the call site.

    Examples
    --------
    >>> Quantity.shares(100) + Quantity.shares(50)
    Quantity(150)
    >>> Quantity.lots(3, lot_size=75)
    Quantity(225)
    >>> Quantity.shares(-1)
    Traceback (most recent call last):
        ...
    dhruva.shared.errors.taxonomy.InvariantViolation: [DHR-SAF-004] quantity must not be negative
    """

    __slots__ = ("_units",)

    _units: int

    def __init__(self, units: int, /) -> None:
        """Wrap a non-negative unit count.

        Raises
        ------
        InvariantViolation
            If ``units`` is negative, a ``bool``, or not an ``int``.
        """
        _reject_non_integer(units, "Quantity")
        invariant(units >= 0, "quantity must not be negative; use Side for direction", units=units)
        object.__setattr__(self, "_units", units)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; value objects are frozen."""
        msg = f"Quantity is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def shares(cls, count: int, /) -> Self:
        """Build a quantity of equity shares."""
        return cls(count)

    @classmethod
    def contracts(cls, count: int, /) -> Self:
        """Build a quantity of derivative contracts.

        Note that one contract is one *unit* of the underlying, not one lot. For
        lots, use :meth:`lots`.
        """
        return cls(count)

    @classmethod
    def lots(cls, count: int, /, *, lot_size: int) -> Self:
        """Build a quantity from a number of lots and the instrument's lot size.

        ``lot_size`` is required and has no default. F&O quantities are quoted in
        lots but transmitted in units, and the gap between "3 lots" and "3
        contracts" is a 25-fold position error for a NIFTY lot of 75.

        Raises
        ------
        InvariantViolation
            If ``lot_size`` is not a positive integer.
        """
        _reject_non_integer(count, "Quantity.lots count")
        _reject_non_integer(lot_size, "Quantity.lots lot_size")
        invariant(lot_size > 0, "lot size must be positive", lot_size=lot_size)
        return cls(count * lot_size)

    @classmethod
    def zero(cls) -> Self:
        """Return a zero quantity."""
        return cls(0)

    # -- accessors ---------------------------------------------------------- #

    @property
    def units(self) -> int:
        """Return the absolute unit count."""
        return self._units

    @property
    def is_zero(self) -> bool:
        """Return whether this quantity is zero."""
        return self._units == 0

    def in_lots(self, *, lot_size: int) -> int:
        """Return this quantity expressed in whole lots.

        Raises
        ------
        InvariantViolation
            If the quantity is not a whole multiple of ``lot_size``. A partial
            lot is not a rounding question; it is an order the exchange will
            reject, and it should surface here rather than there.
        """
        invariant(lot_size > 0, "lot size must be positive", lot_size=lot_size)
        invariant(
            self._units % lot_size == 0,
            "quantity is not a whole number of lots",
            units=self._units,
            lot_size=lot_size,
        )
        return self._units // lot_size

    def signed(self, side: Side, /) -> SignedQuantity:
        """Apply a direction, producing a signed magnitude."""
        return SignedQuantity(self._units * side.sign)

    # -- arithmetic --------------------------------------------------------- #

    def __add__(self, other: Quantity, /) -> Quantity:
        """Add two quantities."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self._units + other._units)

    def __sub__(self, other: Quantity, /) -> Quantity:
        """Subtract, raising if the result would be negative.

        Deliberately not clamped at zero. Subtracting more than is held is a
        logic error somewhere upstream, and silently producing zero would hide it.
        """
        if not isinstance(other, Quantity):
            return NotImplemented
        invariant(
            self._units >= other._units,
            "quantity subtraction would produce a negative quantity",
            minuend=self._units,
            subtrahend=other._units,
        )
        return Quantity(self._units - other._units)

    def __mul__(self, other: int, /) -> Quantity:
        """Scale by a non-negative whole number."""
        if not isinstance(other, int) or isinstance(other, bool):
            return NotImplemented
        return Quantity(self._units * other)

    __rmul__ = __mul__

    # -- comparison and identity -------------------------------------------- #

    def __eq__(self, other: object, /) -> bool:
        """Compare by unit count."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return self._units == other._units

    def __lt__(self, other: Quantity, /) -> bool:
        """Order by unit count."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return self._units < other._units

    def __le__(self, other: Quantity, /) -> bool:
        """Order by unit count."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return self._units <= other._units

    def __gt__(self, other: Quantity, /) -> bool:
        """Order by unit count."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return self._units > other._units

    def __ge__(self, other: Quantity, /) -> bool:
        """Order by unit count."""
        if not isinstance(other, Quantity):
            return NotImplemented
        return self._units >= other._units

    def __bool__(self) -> bool:
        """Return whether this quantity is non-zero."""
        return self._units != 0

    def __hash__(self) -> int:
        """Hash by unit count."""
        return hash(("Quantity", self._units))

    def __str__(self) -> str:
        """Return the unit count as a plain number."""
        return str(self._units)

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return f"Quantity({self._units})"


class SignedQuantity:
    """A quantity with a direction, for position deltas and net changes.

    Deliberately a separate type from :class:`Quantity` rather than a mode of it.
    An order size can never be negative; a position delta can. Giving them the
    same type would mean every function receiving one has to establish which it
    is holding.
    """

    __slots__ = ("_units",)

    _units: int

    def __init__(self, units: int, /) -> None:
        """Wrap a signed unit count."""
        _reject_non_integer(units, "SignedQuantity")
        object.__setattr__(self, "_units", units)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; value objects are frozen."""
        msg = f"SignedQuantity is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    @classmethod
    def of(cls, quantity: Quantity, side: Side, /) -> Self:
        """Build from an unsigned quantity and an explicit side."""
        return cls(quantity.units * side.sign)

    @classmethod
    def zero(cls) -> Self:
        """Return a zero delta, which has no direction."""
        return cls(0)

    @property
    def units(self) -> int:
        """Return the signed unit count."""
        return self._units

    @property
    def magnitude(self) -> Quantity:
        """Return the absolute size, discarding direction."""
        return Quantity(abs(self._units))

    @property
    def side(self) -> Side | None:
        """Return the implied direction, or ``None`` for a zero delta.

        ``None`` rather than a default is deliberate: a zero delta genuinely has
        no direction, and inventing one would be the same category of mistake
        this class exists to prevent.
        """
        if self._units == 0:
            return None
        return Side.BUY if self._units > 0 else Side.SELL

    @property
    def is_zero(self) -> bool:
        """Return whether this delta is zero."""
        return self._units == 0

    def __add__(self, other: SignedQuantity, /) -> SignedQuantity:
        """Add two deltas, as when netting fills into a position."""
        if not isinstance(other, SignedQuantity):
            return NotImplemented
        return SignedQuantity(self._units + other._units)

    def __sub__(self, other: SignedQuantity, /) -> SignedQuantity:
        """Subtract one delta from another."""
        if not isinstance(other, SignedQuantity):
            return NotImplemented
        return SignedQuantity(self._units - other._units)

    def __neg__(self) -> SignedQuantity:
        """Return the delta that reverses this one."""
        return SignedQuantity(-self._units)

    def __abs__(self) -> SignedQuantity:
        """Return the magnitude, still as a signed type."""
        return SignedQuantity(abs(self._units))

    def __eq__(self, other: object, /) -> bool:
        """Compare by signed unit count."""
        if not isinstance(other, SignedQuantity):
            return NotImplemented
        return self._units == other._units

    def __lt__(self, other: SignedQuantity, /) -> bool:
        """Order by signed unit count."""
        if not isinstance(other, SignedQuantity):
            return NotImplemented
        return self._units < other._units

    def __bool__(self) -> bool:
        """Return whether this delta is non-zero."""
        return self._units != 0

    def __hash__(self) -> int:
        """Hash by signed unit count."""
        return hash(("SignedQuantity", self._units))

    def __str__(self) -> str:
        """Return the signed count, with an explicit plus for positive values."""
        return f"{self._units:+d}" if self._units else "0"

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return f"SignedQuantity({self._units})"


def _reject_non_integer(value: object, what: str) -> None:
    """Reject floats and bools before they become unit counts.

    ``bool`` is excluded explicitly because it is a subclass of ``int``, and
    ``Quantity(True)`` meaning one share is not a reading anyone intends.
    """
    invariant(
        not isinstance(value, float),
        f"{what} cannot be built from a float",
        value=repr(value),
    )
    invariant(
        isinstance(value, int) and not isinstance(value, bool),
        f"{what} requires an int",
        value=repr(value),
        actual_type=type(value).__name__,
    )
