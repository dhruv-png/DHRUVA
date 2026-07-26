"""Currency and its minor-unit scale.

Currency is a **runtime attribute**, not a type parameter (Design Review Q2). No
multi-currency trading is planned; parameterising ``Money[INR]`` would cost
readability permanently to gain a compile-time check that a runtime check already
provides. Revisit through an ADR if multi-currency portfolios ever become a
feature.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = ["Currency"]


class Currency(StrEnum):
    """A settlement currency.

    Attributes
    ----------
    INR
        Indian rupee. Minor unit: paise, 2 decimal places.
    """

    INR = "INR"

    @property
    def minor_unit_scale(self) -> int:
        """Return the number of decimal places in this currency's minor unit.

        Returns
        -------
        int
            ``2`` for INR — one rupee is one hundred paise.
        """
        scale: int = _MINOR_UNIT_SCALE[self]
        return scale

    @property
    def minor_units_per_major(self) -> int:
        """Return how many minor units make one major unit (100 for INR)."""
        per_major: int = 10**self.minor_unit_scale
        return per_major

    @property
    def symbol(self) -> str:
        """Return the display symbol."""
        symbol: str = _SYMBOL[self]
        return symbol


_MINOR_UNIT_SCALE: Final[dict[Currency, int]] = {Currency.INR: 2}
_SYMBOL: Final[dict[Currency, str]] = {Currency.INR: "₹"}
