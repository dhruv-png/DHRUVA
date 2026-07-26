"""Prices.

A price is money *per unit*, which makes it dimensionally different from money
(ADR-043). ``Price + Money`` is a category error, and the type system refuses it.

Scale
-----
``Price`` holds an integer at a **fixed scale of 8 decimal places**, chosen once
and never varied by instrument (Design Review Q1). The reasoning:

* NSE equities and options quote to 2 decimal places.
* NSE currency derivatives quote to **4**, ticking at ₹0.0025. A price fixed at
  paise scale would silently round every one of them — invisibly, until somebody
  reconciled a position.
* FX spot conventionally uses 5. Eight leaves headroom for exchanges this
  platform does not yet support.
* At 8 places a ``BIGINT`` still represents prices up to roughly ₹92 billion,
  five orders of magnitude beyond anything traded.

Instrument metadata — tick size, display precision — governs *validation and
formatting* (S07). It never changes the internal representation, so a price means
the same thing everywhere in the system regardless of which instrument produced it.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import ClassVar, Final, Self

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.invariants import invariant
from dhruva.shared.money.currency import Currency
from dhruva.shared.money.money import Money
from dhruva.shared.money.quantity import Quantity
from dhruva.shared.money.rounding import EXACT, RoundingPolicy

__all__ = ["Price"]

_PRICE_PATTERN: Final = re.compile(r"^(?P<sign>[+-]?)(?P<major>\d+)(?:\.(?P<minor>\d+))?$")


class Price:
    """An exact price per unit, in a single currency.

    Examples
    --------
    >>> Price.parse("1234.5678")
    Price('1234.56780000', 'INR')
    >>> Price.parse("83.2525")  # a USD/INR futures price
    Price('83.25250000', 'INR')
    >>> from dhruva.shared.money.quantity import Quantity
    >>> Price.parse("100.50") * Quantity.shares(200)
    Money('20100.00', 'INR')
    """

    #: Decimal places in the internal representation. Fixed, never per-instrument.
    SCALE: ClassVar[int] = 8

    #: Scaled units per whole currency unit.
    UNITS_PER_MAJOR: ClassVar[int] = 10**SCALE

    __slots__ = ("_currency", "_scaled_units")

    _scaled_units: int
    _currency: Currency

    def __init__(self, scaled_units: int, currency: Currency = Currency.INR, /) -> None:
        """Wrap an exact count of scaled units at :attr:`SCALE` decimal places.

        Raises
        ------
        InvariantViolation
            If ``scaled_units`` is a ``float``, a ``bool``, or not an ``int``.
        """
        # See the note in Money.__init__: hot-path guards check first and build
        # the message only on failure.
        if type(scaled_units) is not int:
            raise InvariantViolation(
                "Price requires an int count of scaled units; float is never permitted",
                value=repr(scaled_units),
                actual_type=type(scaled_units).__name__,
            )
        object.__setattr__(self, "_scaled_units", scaled_units)
        object.__setattr__(self, "_currency", currency)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; value objects are frozen."""
        msg = f"Price is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def from_scaled_units(cls, scaled_units: int, currency: Currency = Currency.INR) -> Self:
        """Build from the internal representation. This is also what is persisted."""
        return cls(scaled_units, currency)

    @classmethod
    def parse(cls, text: str, currency: Currency = Currency.INR) -> Self:
        """Parse a decimal string such as ``"1234.56"`` or ``"83.2525"``.

        Raises
        ------
        InvariantViolation
            If malformed, or carrying more than :attr:`SCALE` decimal places.
            Excess precision raises rather than rounding, for the same reason as
            :meth:`Money.parse`: discarding it silently would confirm a belief
            about the value that is not true.
        """
        match = _PRICE_PATTERN.match(text.strip())
        if match is None:
            raise InvariantViolation("price string is malformed", text=text)

        minor_text = match.group("minor") or ""
        invariant(
            len(minor_text) <= cls.SCALE,
            "price string has more decimal places than the price scale supports",
            text=text,
            supported_places=cls.SCALE,
        )
        magnitude = int(match.group("major")) * cls.UNITS_PER_MAJOR + int(
            minor_text.ljust(cls.SCALE, "0") or 0
        )
        return cls(-magnitude if match.group("sign") == "-" else magnitude, currency)

    @classmethod
    def zero(cls, currency: Currency = Currency.INR) -> Self:
        """Return a zero price."""
        return cls(0, currency)

    # -- accessors ---------------------------------------------------------- #

    @property
    def scaled_units(self) -> int:
        """Return the exact count of scaled units. This is what is persisted."""
        return self._scaled_units

    @property
    def currency(self) -> Currency:
        """Return the currency."""
        return self._currency

    @property
    def is_zero(self) -> bool:
        """Return whether the price is exactly zero."""
        return self._scaled_units == 0

    def as_decimal(self) -> Decimal:
        """Return the price as a Decimal, for formatting and reporting only."""
        return Decimal(self._scaled_units).scaleb(-self.SCALE)

    # -- arithmetic --------------------------------------------------------- #

    def notional(self, quantity: Quantity, rounding: RoundingPolicy) -> Money:
        """Multiply by a quantity to obtain consideration (ADR-043).

        Parameters
        ----------
        quantity
            Number of units.
        rounding
            Required. Price carries more decimal places than money does, so the
            product must be rounded to the currency's minor unit, and which rule
            applies is a decision for the caller (ADR-044).
        """
        scale_gap = 10 ** (self.SCALE - self._currency.minor_unit_scale)
        numerator = self._scaled_units * quantity.units
        if numerator < 0:
            return Money(-rounding.apply(-numerator, scale_gap), self._currency)
        return Money(rounding.apply(numerator, scale_gap), self._currency)

    def __mul__(self, other: Quantity, /) -> Money:
        """Multiply by a quantity, refusing to round.

        Uses :data:`~dhruva.shared.money.rounding.EXACT`, so the dimensional
        operation stays expressible (ADR-043) while never rounding silently
        (ADR-044). If the product is not exactly representable in minor units it
        raises, and the message names :meth:`notional` as the way to choose a
        policy.
        """
        if not isinstance(other, Quantity):
            return NotImplemented
        return self.notional(other, EXACT)

    __rmul__ = __mul__

    def __add__(self, other: Price, /) -> Price:
        """Add two prices, as when building a synthetic from legs."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "add")
        return Price(self._scaled_units + other._scaled_units, self._currency)

    def __sub__(self, other: Price, /) -> Price:
        """Subtract prices. A spread between two prices is itself a price."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "subtract")
        return Price(self._scaled_units - other._scaled_units, self._currency)

    def __neg__(self) -> Price:
        """Return the additive inverse."""
        return Price(-self._scaled_units, self._currency)

    def __abs__(self) -> Price:
        """Return the magnitude."""
        return Price(abs(self._scaled_units), self._currency)

    def round_to_tick(self, tick_size: Price, rounding: RoundingPolicy) -> Price:
        """Snap to a multiple of ``tick_size``.

        ``tick_size`` comes from instrument metadata (S07) and is passed in
        rather than looked up, so the dependency direction stays correct.

        Raises
        ------
        InvariantViolation
            If ``tick_size`` is not positive.
        """
        invariant(
            tick_size._scaled_units > 0, "tick size must be positive", tick_size=str(tick_size)
        )
        self._require_same_currency(tick_size, "align")
        ticks = rounding.apply(abs(self._scaled_units), tick_size._scaled_units)
        magnitude = ticks * tick_size._scaled_units
        return Price(-magnitude if self._scaled_units < 0 else magnitude, self._currency)

    # -- comparison and identity -------------------------------------------- #

    def _require_same_currency(self, other: Price, operation: str) -> None:
        """Reject mixed-currency arithmetic.

        Explicit branch rather than ``invariant()``; see the note in
        :meth:`Money._require_same_currency`.
        """
        if self._currency is not other._currency:
            raise InvariantViolation(
                f"cannot {operation} prices in different currencies",
                left=str(self._currency),
                right=str(other._currency),
            )

    def __eq__(self, other: object, /) -> bool:
        """Compare by scaled units and currency."""
        if not isinstance(other, Price):
            return NotImplemented
        return self._scaled_units == other._scaled_units and self._currency is other._currency

    def __lt__(self, other: Price, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._scaled_units < other._scaled_units

    def __le__(self, other: Price, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._scaled_units <= other._scaled_units

    def __gt__(self, other: Price, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._scaled_units > other._scaled_units

    def __ge__(self, other: Price, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Price):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._scaled_units >= other._scaled_units

    def __bool__(self) -> bool:
        """Return whether the price is non-zero."""
        return self._scaled_units != 0

    def __hash__(self) -> int:
        """Hash by scaled units and currency."""
        return hash(("Price", self._scaled_units, self._currency))

    def __str__(self) -> str:
        """Render at full internal precision, without a currency symbol.

        Display formatting to an instrument's quoted precision is a presentation
        concern and belongs to S07, which knows the instrument.
        """
        return self._formatted()

    def __repr__(self) -> str:
        """Return a representation that round-trips through :meth:`parse`."""
        return f"Price('{self._formatted()}', '{self._currency}')"

    def _formatted(self) -> str:
        """Render the scaled integer as a fixed-point decimal string."""
        sign = "-" if self._scaled_units < 0 else ""
        magnitude = abs(self._scaled_units)
        major, minor = divmod(magnitude, self.UNITS_PER_MAJOR)
        return f"{sign}{major}.{minor:0{self.SCALE}d}"
