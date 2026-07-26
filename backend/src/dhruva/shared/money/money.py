"""Monetary amounts.

``Money`` is an exact integer count of minor units — paise for INR — carried
alongside its currency (ADR-042). :class:`~decimal.Decimal` appears only at the
parsing and formatting boundary; it never carries state.

Three reasons this is an integer rather than a ``Decimal``:

* **Determinism.** ``Decimal`` arithmetic reads a thread-local context, so two
  processes configured differently produce different answers from identical
  inputs. That is a determinism defect one layer below ADR-011.
* **Fidelity.** We persist ``BIGINT`` minor units. An integer needs no
  conversion, so no conversion can be wrong.
* **Speed.** A backtest performs on the order of 10⁸ monetary operations.

``float`` cannot enter this module at any point. Boundary rule R6 enforces that
mechanically; the constructors reject it at runtime as well, because a value that
reaches production through a path the linter did not see should still fail loudly.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Self

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.invariants import invariant
from dhruva.shared.money.currency import Currency
from dhruva.shared.money.ratio import Ratio
from dhruva.shared.money.rounding import EXACT, RoundingPolicy

if TYPE_CHECKING:
    from dhruva.shared.money.price import Price
    from dhruva.shared.money.quantity import Quantity

__all__ = ["Money"]

_AMOUNT_PATTERN: Final = re.compile(r"^(?P<sign>[+-]?)(?P<major>\d+)(?:\.(?P<minor>\d+))?$")


class Money:
    """An exact monetary amount in a single currency.

    Examples
    --------
    >>> Money.of(1234, 56)
    Money('1234.56', 'INR')
    >>> Money.parse("1234.56") + Money.parse("0.44")
    Money('1235.00', 'INR')
    >>> str(Money.parse("1234.56"))
    '₹1234.56'
    >>> Money.parse("100.00").allocate([1, 1, 1])
    [Money('33.34', 'INR'), Money('33.33', 'INR'), Money('33.33', 'INR')]
    """

    __slots__ = ("_currency", "_minor_units")

    _minor_units: int
    _currency: Currency

    def __init__(self, minor_units: int, currency: Currency = Currency.INR, /) -> None:
        """Wrap an exact count of minor units.

        Prefer :meth:`of`, :meth:`parse` or :meth:`from_minor_units` — they say
        which unit the caller is thinking in.

        Raises
        ------
        InvariantViolation
            If ``minor_units`` is a ``float``, a ``bool``, or not an ``int``.
        """
        # `type(x) is int` rather than `isinstance`, deliberately: it rejects
        # bool (a subclass of int) in the same test, and it is a single pointer
        # comparison. The eager-argument form -- invariant(cond, msg, **ctx) --
        # builds a repr and a dict on every construction, which the S03
        # benchmark measured at 0.89us against a 0.30us budget. Guards on hot
        # paths check first and construct the message only on failure.
        if type(minor_units) is not int:
            raise InvariantViolation(
                "Money requires an int count of minor units; float is never permitted",
                value=repr(minor_units),
                actual_type=type(minor_units).__name__,
            )
        object.__setattr__(self, "_minor_units", minor_units)
        object.__setattr__(self, "_currency", currency)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; value objects are frozen."""
        msg = f"Money is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    # -- constructors ------------------------------------------------------- #

    @classmethod
    def from_minor_units(cls, minor_units: int, currency: Currency = Currency.INR) -> Self:
        """Build from a count of paise. This is also the storage representation."""
        return cls(minor_units, currency)

    @classmethod
    def of(cls, major: int, minor: int = 0, currency: Currency = Currency.INR) -> Self:
        """Build from major and minor parts, as in ``Money.of(1234, 56)``.

        For a negative amount, put the sign on ``major``; ``minor`` is always the
        magnitude of the fractional part, so ``Money.of(-1234, 56)`` is
        ``-1234.56`` rather than ``-1233.44``.

        Raises
        ------
        InvariantViolation
            If ``minor`` is negative or exceeds the currency's minor-unit range.
        """
        scale = currency.minor_units_per_major
        invariant(
            0 <= minor < scale,
            "minor part is out of range for the currency",
            minor=minor,
            currency=str(currency),
            limit=scale,
        )
        magnitude = abs(major) * scale + minor
        return cls(-magnitude if major < 0 else magnitude, currency)

    @classmethod
    def parse(cls, text: str, currency: Currency = Currency.INR) -> Self:
        """Parse a decimal string such as ``"1234.56"`` or ``"-0.05"``.

        Raises
        ------
        InvariantViolation
            If the text is malformed, or carries more decimal places than the
            currency has. Excess precision raises rather than rounding: a value
            arriving with more precision than the currency supports means the
            caller believes something about the amount that is not true, and
            silently discarding it would confirm the belief.
        """
        match = _AMOUNT_PATTERN.match(text.strip())
        if match is None:
            raise InvariantViolation("money string is malformed", text=text)

        scale = currency.minor_unit_scale
        minor_text = match.group("minor") or ""
        invariant(
            len(minor_text) <= scale,
            "money string has more decimal places than the currency supports",
            text=text,
            currency=str(currency),
            supported_places=scale,
        )
        magnitude = int(match.group("major")) * currency.minor_units_per_major + int(
            minor_text.ljust(scale, "0") or 0
        )
        return cls(-magnitude if match.group("sign") == "-" else magnitude, currency)

    @classmethod
    def zero(cls, currency: Currency = Currency.INR) -> Self:
        """Return a zero amount."""
        return cls(0, currency)

    # -- accessors ---------------------------------------------------------- #

    @property
    def minor_units(self) -> int:
        """Return the exact count of minor units. This is what is persisted."""
        return self._minor_units

    @property
    def currency(self) -> Currency:
        """Return the currency."""
        return self._currency

    @property
    def is_zero(self) -> bool:
        """Return whether the amount is exactly zero."""
        return self._minor_units == 0

    @property
    def is_negative(self) -> bool:
        """Return whether the amount is below zero."""
        return self._minor_units < 0

    def as_decimal(self) -> Decimal:
        """Return the amount as a Decimal, for formatting and reporting only.

        Never feed this back into arithmetic. Round-tripping through Decimal is
        exactly the thread-local-context exposure ADR-042 removes.
        """
        return Decimal(self._minor_units).scaleb(-self._currency.minor_unit_scale)

    # -- arithmetic --------------------------------------------------------- #

    def __add__(self, other: Money, /) -> Money:
        """Add two amounts of the same currency."""
        if not isinstance(other, Money):
            return NotImplemented
        if self._currency is not other._currency:
            self._reject_currency_mismatch(other, "add")
        return Money(self._minor_units + other._minor_units, self._currency)

    def __sub__(self, other: Money, /) -> Money:
        """Subtract one amount from another of the same currency."""
        if not isinstance(other, Money):
            return NotImplemented
        if self._currency is not other._currency:
            self._reject_currency_mismatch(other, "subtract")
        return Money(self._minor_units - other._minor_units, self._currency)

    def __mul__(self, other: int, /) -> Money:
        """Multiply by a whole number. Exact, so no rounding policy is needed."""
        if not isinstance(other, int) or isinstance(other, bool):
            return NotImplemented
        return Money(self._minor_units * other, self._currency)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        """Return the additive inverse."""
        return Money(-self._minor_units, self._currency)

    def __abs__(self) -> Money:
        """Return the magnitude."""
        return Money(abs(self._minor_units), self._currency)

    def divide(self, divisor: int, rounding: RoundingPolicy) -> Money:
        """Divide by a whole number under an explicit rounding policy (ADR-044)."""
        invariant(
            isinstance(divisor, int) and not isinstance(divisor, bool),
            "Money can only be divided by an int",
            value=repr(divisor),
        )
        invariant(divisor != 0, "cannot divide money by zero")
        numerator, denominator = (
            (-self._minor_units, -divisor) if divisor < 0 else (self._minor_units, divisor)
        )
        return Money(rounding.apply(numerator, denominator), self._currency)

    def __truediv__(self, other: int, /) -> Money:
        """Divide by a whole number, refusing to round.

        Bare division uses :data:`~dhruva.shared.money.rounding.EXACT`, so the
        operation stays expressible (ADR-043) but raises rather than rounding
        silently (ADR-044). Use :meth:`divide` with a named policy instead.
        """
        if not isinstance(other, int) or isinstance(other, bool):
            return NotImplemented
        return self.divide(other, EXACT)

    def apply(self, rate: Ratio, rounding: RoundingPolicy) -> Money:
        """Apply a rate, as when computing a charge or a tax.

        Parameters
        ----------
        rate
            The proportion to apply. ``Ratio.from_percent(18)`` for GST.
        rounding
            Required. Which market rule governs this particular charge.

        Notes
        -----
        The rate's Decimal fraction is converted to an exact integer ratio before
        any division, so the result does not depend on decimal context precision.
        """
        sign, digits, exponent = rate.fraction.as_tuple()
        if not isinstance(
            exponent, int
        ):  # pragma: no cover - Ratio rejects non-finite at construction
            raise InvariantViolation("rate must have a finite exponent", rate=str(rate))

        unscaled = int("".join(str(d) for d in digits) or "0")
        if sign:
            unscaled = -unscaled

        if exponent >= 0:
            numerator = self._minor_units * unscaled * 10**exponent
            denominator = 1
        else:
            numerator = self._minor_units * unscaled
            denominator = 10**-exponent

        if numerator < 0:
            return Money(-rounding.apply(-numerator, denominator), self._currency)
        return Money(rounding.apply(numerator, denominator), self._currency)

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split this amount across ``weights``, losing nothing.

        Uses largest-remainder distribution, so ``sum(result) == self`` exactly
        for any weights. Splitting ₹100 three ways gives ₹33.34, ₹33.33, ₹33.33 —
        not three lots of ₹33.33 and a vanished paisa.

        This matters because the platform apportions brokerage across legs, STT
        across fills, and P&L across lots. Vanishing minor units accumulate into
        reconciliation failures that are genuinely unpleasant to trace.

        Ties are broken by position, so the result is deterministic (ADR-011).

        Raises
        ------
        InvariantViolation
            If ``weights`` is empty, contains a negative, or sums to zero.
        """
        invariant(len(weights) > 0, "allocation requires at least one weight")
        invariant(
            all(w >= 0 for w in weights), "allocation weights must not be negative", weights=weights
        )
        total_weight = sum(weights)
        invariant(total_weight > 0, "allocation weights must not sum to zero", weights=weights)

        sign = -1 if self._minor_units < 0 else 1
        amount = abs(self._minor_units)

        shares: list[int] = []
        remainders: list[int] = []
        for weight in weights:
            share, remainder = divmod(amount * weight, total_weight)
            shares.append(share)
            remainders.append(remainder)

        leftover = amount - sum(shares)
        order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
        for index in order[:leftover]:
            shares[index] += 1

        return [Money(sign * share, self._currency) for share in shares]

    def split(self, parts: int) -> list[Money]:
        """Split into ``parts`` equal shares, losing nothing."""
        invariant(parts > 0, "split requires a positive number of parts", parts=parts)
        return self.allocate([1] * parts)

    def ratio_to(self, other: Money) -> Ratio:
        """Return this amount as a proportion of ``other``.

        Money divided by money is dimensionless, so the result is a
        :class:`~dhruva.shared.money.ratio.Ratio` rather than a ``Money``.
        """
        self._require_same_currency(other, "compare")
        invariant(not other.is_zero, "cannot express a ratio to zero money")
        return Ratio(Decimal(self._minor_units) / Decimal(other._minor_units))

    def per_unit(self, quantity: Quantity, rounding: RoundingPolicy) -> Price:
        """Divide by a quantity to obtain a unit price (ADR-043).

        The typical use is an average fill price: total consideration divided by
        the number of units filled.
        """
        # Imported here rather than at module scope: price.py imports money.py, so
        # a top-level import would be a genuine cycle. This is the only one.
        from dhruva.shared.money.price import Price  # noqa: PLC0415

        invariant(not quantity.is_zero, "cannot compute a unit price for zero quantity")
        scale_gap = 10 ** (Price.SCALE - self._currency.minor_unit_scale)
        numerator = self._minor_units * scale_gap
        if numerator < 0:
            return Price(-rounding.apply(-numerator, quantity.units), self._currency)
        return Price(rounding.apply(numerator, quantity.units), self._currency)

    # -- comparison and identity -------------------------------------------- #

    def _require_same_currency(self, other: Money, operation: str) -> None:
        """Reject mixed-currency arithmetic (Design Review Q2: runtime check)."""
        if self._currency is not other._currency:
            self._reject_currency_mismatch(other, operation)

    def _reject_currency_mismatch(self, other: Money, operation: str) -> None:
        """Raise for a currency mismatch. Cold path, kept out of the hot one.

        The comparison lives at the call site and only the *failure* costs a
        function call. Two earlier shapes were measured and rejected: the eager
        ``invariant(cond, msg, **ctx)`` form built an f-string and two ``str()``
        calls on every addition (1.86us against a 0.50us budget), and routing
        every addition through a checking method still cost a frame (0.53us).
        """
        raise InvariantViolation(
            f"cannot {operation} amounts in different currencies",
            left=str(self._currency),
            right=str(other._currency),
        )

    def __eq__(self, other: object, /) -> bool:
        """Compare by amount and currency. Different currencies are never equal."""
        if not isinstance(other, Money):
            return NotImplemented
        return self._minor_units == other._minor_units and self._currency is other._currency

    def __lt__(self, other: Money, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._minor_units < other._minor_units

    def __le__(self, other: Money, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._minor_units <= other._minor_units

    def __gt__(self, other: Money, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._minor_units > other._minor_units

    def __ge__(self, other: Money, /) -> bool:
        """Order within a currency."""
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other, "compare")
        return self._minor_units >= other._minor_units

    def __bool__(self) -> bool:
        """Return whether the amount is non-zero."""
        return self._minor_units != 0

    def __hash__(self) -> int:
        """Hash by amount and currency."""
        return hash(("Money", self._minor_units, self._currency))

    def __str__(self) -> str:
        """Render with the currency symbol, at the currency's full precision."""
        scale = self._currency.minor_unit_scale
        sign = "-" if self._minor_units < 0 else ""
        magnitude = abs(self._minor_units)
        major, minor = divmod(magnitude, self._currency.minor_units_per_major)
        return f"{sign}{self._currency.symbol}{major}.{minor:0{scale}d}"

    def __repr__(self) -> str:
        """Return a representation that round-trips through :meth:`parse`."""
        scale = self._currency.minor_unit_scale
        sign = "-" if self._minor_units < 0 else ""
        magnitude = abs(self._minor_units)
        major, minor = divmod(magnitude, self._currency.minor_units_per_major)
        return f"Money('{sign}{major}.{minor:0{scale}d}', '{self._currency}')"
