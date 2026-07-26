"""Monetary primitives.

The dimensional model (ADR-043). These are physically different quantities, and
the type system refuses to mix them::

    Price * Quantity --> Money        (rounding policy required)
    Money ÷ Quantity --> Price        (rounding policy required)
    Money * int      --> Money        (exact)
    Money ÷ Money    --> Ratio        (dimensionless)
    Money ± Money    --> Money        (same currency enforced)

Operations that deliberately do not exist: ``Money * Money``, ``Price * Price``,
``Money + Price``, ``Money + int``. Each is a category error that would otherwise
be caught only by a reviewer noticing an implausible number.

No ``float`` appears anywhere in this package, and boundary rule R6 fails the
build if one is introduced (ADR-048).
"""

from __future__ import annotations

from dhruva.shared.money.currency import Currency
from dhruva.shared.money.money import Money
from dhruva.shared.money.price import Price
from dhruva.shared.money.quantity import Quantity, Side, SignedQuantity
from dhruva.shared.money.ratio import Ratio
from dhruva.shared.money.rounding import (
    CHARGE_TO_PAISE,
    CONSERVATIVE_TO_TRADER,
    CTT_NEAREST_RUPEE,
    EXACT,
    GST_TO_PAISE,
    STAMP_DUTY_NEAREST_RUPEE,
    STATISTICAL,
    STT_NEAREST_RUPEE,
    RoundingMode,
    RoundingPolicy,
)

__all__ = [
    "CHARGE_TO_PAISE",
    "CONSERVATIVE_TO_TRADER",
    "CTT_NEAREST_RUPEE",
    "EXACT",
    "GST_TO_PAISE",
    "STAMP_DUTY_NEAREST_RUPEE",
    "STATISTICAL",
    "STT_NEAREST_RUPEE",
    "Currency",
    "Money",
    "Price",
    "Quantity",
    "Ratio",
    "RoundingMode",
    "RoundingPolicy",
    "Side",
    "SignedQuantity",
]
