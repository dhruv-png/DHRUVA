"""Explicit rounding policies (ADR-044).

There is **no default rounding anywhere in this platform**. Every operation that
can lose precision takes a policy, chosen at the call site by someone who knows
which rule applies.

Policies are named after the market rule they implement rather than after the
mathematical mode, because the rule is what a reviewer needs to check against a
SEBI or exchange circular. ``STT_NEAREST_RUPEE`` is checkable against the
Securities Transaction Tax rules; ``ROUND_HALF_UP`` is not.

All rounding here operates on exact integer ratios. Nothing is converted to
``float`` at any point, and nothing depends on :mod:`decimal`'s thread-local
context (ADR-042).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.invariants import invariant

__all__ = [
    "CHARGE_TO_PAISE",
    "CONSERVATIVE_TO_TRADER",
    "CTT_NEAREST_RUPEE",
    "EXACT",
    "GST_TO_PAISE",
    "STAMP_DUTY_NEAREST_RUPEE",
    "STATISTICAL",
    "STT_NEAREST_RUPEE",
    "RoundingMode",
    "RoundingPolicy",
]


class RoundingMode(StrEnum):
    """How to resolve a value that falls between two representable ones."""

    HALF_UP = "half_up"
    """Ties round away from zero. The convention in Indian tax and charge rules."""

    HALF_EVEN = "half_even"
    """Ties round to the even neighbour. Unbiased over many operations."""

    DOWN = "down"
    """Truncate toward zero."""

    UP = "up"
    """Away from zero. Used where an estimate must not flatter the trader."""

    FLOOR = "floor"
    """Toward negative infinity."""

    CEILING = "ceiling"
    """Toward positive infinity."""

    EXACT = "exact"
    """Refuse to round. Raises if the value is not exactly representable."""


@dataclass(frozen=True, slots=True)
class RoundingPolicy:
    """A named rounding rule.

    Attributes
    ----------
    name
        Identifies the market rule, for traceability in reviews and audits.
    mode
        How ties and remainders resolve.
    quantum
        The granularity to round to, expressed in minor units. ``1`` rounds to
        the minor unit (paise). ``100`` rounds to whole rupees, which is what
        Securities Transaction Tax requires.
    """

    name: str
    mode: RoundingMode
    quantum: int = 1

    def __post_init__(self) -> None:
        """Reject a policy that could not round anything sensibly."""
        invariant(
            self.quantum >= 1,
            "rounding quantum must be at least one minor unit",
            policy=self.name,
            quantum=self.quantum,
        )

    def __str__(self) -> str:
        """Return the policy name, which is what belongs in a log or an audit line."""
        return self.name

    def apply(self, numerator: int, denominator: int) -> int:
        """Round the exact ratio ``numerator / denominator`` to this policy.

        Parameters
        ----------
        numerator, denominator
            Exact integers. ``denominator`` must be positive.

        Returns
        -------
        int
            The rounded value, in minor units, snapped to :attr:`quantum`.

        Raises
        ------
        InvariantViolation
            If ``denominator`` is not positive, or if the mode is
            :attr:`RoundingMode.EXACT` and the value is not exactly representable.

        Notes
        -----
        Implemented entirely in integer arithmetic. Converting to ``float`` here
        would defeat the purpose of the whole money design, and converting to
        ``Decimal`` would reintroduce the thread-local context dependence that
        ADR-042 exists to remove.
        """
        invariant(
            denominator > 0,
            "rounding denominator must be positive",
            numerator=numerator,
            denominator=denominator,
        )

        if self.quantum > 1:
            # Round to a coarser granularity by folding the quantum into the
            # denominator, then scaling the result back up. This keeps the whole
            # operation in one exact integer division.
            scaled = self._divide(numerator, denominator * self.quantum)
            return scaled * self.quantum
        return self._divide(numerator, denominator)

    def _divide(self, numerator: int, denominator: int) -> int:
        """Perform one exact integer division under this policy's mode."""
        quotient, remainder = divmod(numerator, denominator)
        if remainder == 0:
            return quotient

        match self.mode:
            case RoundingMode.EXACT:
                raise _inexact(numerator, denominator, self.name)
            case RoundingMode.FLOOR:
                return quotient
            case RoundingMode.CEILING:
                return quotient + 1
            case RoundingMode.DOWN:
                return quotient + 1 if numerator < 0 else quotient
            case RoundingMode.UP:
                return quotient if numerator < 0 else quotient + 1
            case RoundingMode.HALF_UP | RoundingMode.HALF_EVEN:
                return self._round_half(quotient, remainder, denominator, numerator)
            case _:  # pragma: no cover - closed enum, exhaustively matched above
                raise InvariantViolation("unhandled rounding mode", mode=self.mode)

    def _round_half(self, quotient: int, remainder: int, denominator: int, numerator: int) -> int:
        """Resolve a tie or a partial remainder for the two half-* modes.

        ``divmod`` floors, so ``remainder`` is always in ``[0, denominator)`` and
        ``quotient`` is the floor. That makes "is the true value above, below or
        exactly at the midpoint?" a comparison of ``2 * remainder`` against
        ``denominator``.
        """
        doubled = remainder * 2
        if doubled > denominator:
            return quotient + 1
        if doubled < denominator:
            return quotient
        # Exactly at the midpoint.
        if self.mode is RoundingMode.HALF_EVEN:
            return quotient if quotient % 2 == 0 else quotient + 1
        # HALF_UP means away from zero, and the floor sits below the true value.
        return quotient if numerator < 0 else quotient + 1


def _inexact(numerator: int, denominator: int, policy: str) -> InvariantViolation:
    """Build the error raised when EXACT rounding meets an inexact value."""
    return InvariantViolation(
        "operation is not exact; supply a rounding policy at the call site",
        numerator=numerator,
        denominator=denominator,
        policy=policy,
    )


#: Refuses to round. This is what bare operators such as ``Price * Quantity``
#: use, so that dimensional arithmetic stays expressible (ADR-043) while never
#: rounding silently (ADR-044). If the result is not exact it raises, and the
#: message names the explicit-policy method to call instead.
EXACT: Final = RoundingPolicy("EXACT", RoundingMode.EXACT)

#: Securities Transaction Tax is charged rounded to the nearest rupee.
STT_NEAREST_RUPEE: Final = RoundingPolicy("STT_NEAREST_RUPEE", RoundingMode.HALF_UP, quantum=100)

#: Commodities Transaction Tax follows the same rounding as STT.
CTT_NEAREST_RUPEE: Final = RoundingPolicy("CTT_NEAREST_RUPEE", RoundingMode.HALF_UP, quantum=100)

#: Brokerage, exchange transaction charges and SEBI turnover fees: two decimals.
CHARGE_TO_PAISE: Final = RoundingPolicy("CHARGE_TO_PAISE", RoundingMode.HALF_UP)

#: GST on charges, rounded to paise.
GST_TO_PAISE: Final = RoundingPolicy("GST_TO_PAISE", RoundingMode.HALF_UP)

#: Stamp duty, rounded to the nearest rupee under most state schedules.
STAMP_DUTY_NEAREST_RUPEE: Final = RoundingPolicy(
    "STAMP_DUTY_NEAREST_RUPEE", RoundingMode.HALF_UP, quantum=100
)

#: For **pre-trade estimates only**. Rounds away from zero, so an estimated cost
#: is never understated and an estimated proceed is never overstated.
#:
#: This exists because optimistic cost estimates are one of the quieter ways a
#: strategy appears profitable and is not. Settlement always uses the exchange's
#: actual rule; this is for the number shown before the trade.
CONSERVATIVE_TO_TRADER: Final = RoundingPolicy("CONSERVATIVE_TO_TRADER", RoundingMode.UP)

#: Unbiased rounding for analytics and reporting, where no market rule applies
#: and repeated rounding should not drift.
STATISTICAL: Final = RoundingPolicy("STATISTICAL", RoundingMode.HALF_EVEN)
