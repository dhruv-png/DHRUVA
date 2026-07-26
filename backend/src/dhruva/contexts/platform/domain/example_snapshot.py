"""A worked example aggregate, deliberately throwaway.

Exists so S04 can prove the four-layer persistence flow end to end against
something real, without pre-empting design decisions that belong to S07 or S11.
**Deleted when the first genuine aggregate arrives.**

It is chosen to exercise every hard case at once: a ``Money``, a ``Price``, an
``InstrumentId``, and a ``TradingDay`` -- the last being the one that cannot be
reconstructed without a calendar (ADR-046), which is what forced the
reconstruction-factory layer.

Note what this module does **not** import. No SQLAlchemy, no session, no column,
no framework of any kind. Boundary rule R7 fails the build if that changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.shared.money import Money, Price
from dhruva.shared.time import TradingDay

__all__ = ["DailySnapshot"]


@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """One instrument's close for one trading day.

    Attributes
    ----------
    instrument_id, trading_day
        Together the natural key. Distinct from the surrogate primary key, which
        is a persistence concern the domain does not carry.
    close
        The closing price.
    turnover
        Value traded, in settled currency.
    account_id
        Present on every aggregate from day one (ADR-004), so no row has to be
        retrofitted when multi-tenancy activates at S44.
    version
        Optimistic-concurrency token (ADR-057). Carried by the domain object
        because a caller needs to see the conflict, not because the domain cares
        how rows are locked.
    """

    instrument_id: InstrumentId
    trading_day: TradingDay
    close: Price
    turnover: Money
    account_id: AccountId
    version: int = 1

    def __post_init__(self) -> None:
        """Reject a snapshot that could not describe a real session."""
        # Checked first, context built only on failure. An aggregate is
        # constructed on every row read, and str(instrument_id) plus a date
        # isoformat on each one measured a third of the reconstruction budget at
        # S04. The eager invariant() helper stays correct for cold paths.
        if self.close.is_zero:
            raise InvariantViolation(
                "a snapshot close price must be non-zero",
                instrument_id=str(self.instrument_id),
                trading_day=self.trading_day.iso,
            )
        if self.turnover.is_negative:
            raise InvariantViolation("turnover cannot be negative", turnover=str(self.turnover))
        if self.version < 1:
            raise InvariantViolation("version starts at one", version=self.version)

    def revise_close(self, close: Price) -> DailySnapshot:
        """Return a snapshot with a corrected close, advancing the version.

        Returns a new instance rather than mutating: the aggregate is frozen, and
        an exchange revising a close is a new fact rather than an edit to the old
        one.
        """
        return replace(self, close=close, version=self.version + 1)
