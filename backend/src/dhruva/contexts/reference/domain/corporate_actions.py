"""Provider-neutral corporate-action and explicit return-basis evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "CorporateActionEvidence",
    "CorporateActionType",
    "EvidenceVerification",
    "ReturnBasis",
    "return_basis_supported",
]


class CorporateActionType(StrEnum):
    """Capital and lifecycle events relevant to longitudinal research."""

    SPLIT = "SPLIT"
    BONUS = "BONUS"
    DIVIDEND = "DIVIDEND"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    MERGER = "MERGER"
    DEMERGER = "DEMERGER"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


class EvidenceVerification(StrEnum):
    """How strongly the event is supported by its named source."""

    UNVERIFIED = "UNVERIFIED"
    SOURCE_REPORTED = "SOURCE_REPORTED"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"


class ReturnBasis(StrEnum):
    """Meaning of a security return; never inferred from attractive numbers."""

    RAW_PRICE = "RAW_PRICE"
    PRICE_ADJUSTED = "PRICE_ADJUSTED"
    TOTAL_RETURN = "TOTAL_RETURN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CorporateActionEvidence:
    """One append-only, revisioned event observation."""

    instrument_id: InstrumentId
    event_type: CorporateActionType
    effective_date: date
    ex_date: date | None
    record_date: date | None
    known_at: datetime
    source: str
    source_revision: str
    verification: EvidenceVerification
    ratio_numerator: Decimal | None = None
    ratio_denominator: Decimal | None = None
    cash_value: Decimal | None = None
    currency: str | None = None
    related_instrument_id: InstrumentId | None = None

    def __post_init__(self) -> None:
        """Reject incomplete ratios, cash values, and knowledge time."""
        invariant(
            self.known_at.tzinfo is not None and self.known_at.utcoffset() is not None,
            "corporate action known_at needs zone",
        )
        invariant(self.known_at.utcoffset() == timedelta(0), "corporate action known_at not UTC")
        ratio = (self.ratio_numerator, self.ratio_denominator)
        invariant((ratio[0] is None) == (ratio[1] is None), "corporate action ratio is partial")
        if ratio[0] is not None:
            invariant(ratio[0] > 0 and ratio[1] is not None and ratio[1] > 0, "invalid ratio")
        invariant((self.cash_value is None) == (self.currency is None), "cash value needs currency")
        if self.cash_value is not None:
            invariant(self.cash_value >= 0, "corporate action cash value is negative")
        invariant(self.source.strip() != "" and self.source_revision.strip() != "", "source blank")


def return_basis_supported(
    basis: ReturnBasis,
    *,
    adjustment_verified: bool,
    dividends_complete: bool,
) -> bool:
    """Fail closed when evidence cannot support the requested return meaning."""
    if basis is ReturnBasis.RAW_PRICE:
        return True
    if basis is ReturnBasis.PRICE_ADJUSTED:
        return adjustment_verified
    if basis is ReturnBasis.TOTAL_RETURN:
        return adjustment_verified and dividends_complete
    return False
