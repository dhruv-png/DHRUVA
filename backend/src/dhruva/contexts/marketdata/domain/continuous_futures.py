"""Deterministic futures roll policy and the continuous research series.

A continuous series is **research output, not an instrument.** It is stitched
from actual contracts so that a trend, an average or a volatility estimate can
span an expiry without a false gap. Nothing trades it: it has no exchange, no
token, no lot and no member of
:class:`~dhruva.contexts.marketdata.domain.daily_bars.MarketInstrumentKind`, so
it cannot be persisted as a daily bar or requested from a provider. Every bar
names the actual contract it came from, and
:meth:`ContinuousFuturesSeries.execution_contract_on` is the only sanctioned way
to get from a research date to something a simulated fill may use.

The roll is decided from data that existed on the decision session and takes
effect on the *next* session. A policy that switched contracts on the same
session it decided to would be reading a bar it had already traded through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.domain.daily_bars import (
    BarCompleteness,
    DailyBarSeries,
    DailyCandle,
)
from dhruva.shared.errors import MissingDataError, ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "CONTINUOUS_FUTURES_POLICY_REVISION",
    "ContinuousBar",
    "ContinuousFuturesSeries",
    "ExecutionContract",
    "FuturesContractTerms",
    "FuturesRoll",
    "RollPolicy",
    "RollReason",
    "build_continuous_series",
]

#: Policy identity recorded on every series and every roll it produces.
#:
#: Changing the rule below changes this string. A stored research result that
#: names an older revision is then visibly the output of a different policy
#: rather than silently reinterpreted by the current one.
CONTINUOUS_FUTURES_POLICY_REVISION = "continuous-futures-roll-v1"

_POLICY_REVISION = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")


class RollReason(StrEnum):
    """Why the series stopped using one contract and began using the next."""

    LIQUIDITY_CROSSOVER = "LIQUIDITY_CROSSOVER"
    EXPIRY_FALLBACK = "EXPIRY_FALLBACK"


@dataclass(frozen=True, slots=True)
class FuturesContractTerms:
    """The exchange facts about one actual contract the roll policy needs.

    Deliberately not the reference context's ``FuturesContract``: the roll rule
    is arithmetic over expiries, lots and liquidity, and keeping it free of
    another context's type makes it testable without that context's fixtures.
    """

    contract_id: InstrumentId
    instrument_token: int
    expiry: date
    lot_size: int

    def __post_init__(self) -> None:
        """Reject terms that cannot describe a real listed contract."""
        invariant(self.instrument_token > 0, "instrument token must be positive")
        invariant(self.lot_size > 0, "futures lot size must be positive")


@dataclass(frozen=True, slots=True)
class RollPolicy:
    """Versioned, deterministic rule for moving from one contract to the next."""

    expiry_buffer_sessions: int = 2
    revision: str = CONTINUOUS_FUTURES_POLICY_REVISION

    def __post_init__(self) -> None:
        """Require a usable buffer and an identifiable revision."""
        invariant(self.expiry_buffer_sessions >= 0, "expiry buffer cannot be negative")
        invariant(bool(_POLICY_REVISION.fullmatch(self.revision)), "invalid roll policy revision")


@dataclass(frozen=True, slots=True)
class ContinuousBar:
    """One research bar, and the actual contract whose prices produced it.

    Carries no instrument kind and no identity of its own. The stitched series
    is not a thing that exists on an exchange, and the type refuses to pretend
    otherwise.
    """

    trading_date: date
    candle: DailyCandle
    source_contract_id: InstrumentId
    source_instrument_token: int
    source_expiry: date
    source_lot_size: int

    def __post_init__(self) -> None:
        """Keep every bar attributable to a contract that was still alive."""
        invariant(
            self.trading_date == self.candle.trading_date,
            "continuous bar date must match its candle",
        )
        invariant(
            self.trading_date <= self.source_expiry,
            "a continuous bar cannot come from an expired contract",
        )
        invariant(self.source_instrument_token > 0, "source instrument token must be positive")
        invariant(self.source_lot_size > 0, "source lot size must be positive")


@dataclass(frozen=True, slots=True)
class FuturesRoll:
    """One recorded switch of source contract, with the evidence behind it."""

    decision_date: date
    roll_date: date
    reason: RollReason
    from_contract_id: InstrumentId
    to_contract_id: InstrumentId
    from_expiry: date
    to_expiry: date
    from_close: Decimal
    to_close: Decimal
    from_volume: int
    to_volume: int
    from_open_interest: int | None
    to_open_interest: int | None
    policy_revision: str

    def __post_init__(self) -> None:
        """Require a forward roll decided strictly before it took effect."""
        invariant(self.decision_date < self.roll_date, "a roll takes effect after it is decided")
        invariant(self.from_expiry < self.to_expiry, "a roll must move to a later expiry")
        invariant(
            self.from_contract_id != self.to_contract_id,
            "a roll must change the source contract",
        )
        invariant(self.decision_date <= self.from_expiry, "a roll is decided before front expiry")
        invariant(
            bool(_POLICY_REVISION.fullmatch(self.policy_revision)),
            "invalid roll policy revision",
        )

    @property
    def price_gap(self) -> Decimal:
        """Close-to-close discontinuity the stitch introduces at ``roll_date``.

        Recorded rather than removed. v1 emits unadjusted prices, so the gap is
        visible to anything reading the series; a back-adjustment method would
        be a new policy revision that consumes exactly this number.
        """
        return self.to_close - self.from_close


@dataclass(frozen=True, slots=True)
class ExecutionContract:
    """The actual contract a simulated fill must use on one session.

    Returned only by :meth:`ContinuousFuturesSeries.execution_contract_on`. A
    backtest or paper order sized against a continuous price still fills against
    this, at this lot size.
    """

    trading_date: date
    contract_id: InstrumentId
    instrument_token: int
    expiry: date
    lot_size: int


@dataclass(frozen=True, slots=True)
class ContinuousFuturesSeries:
    """A stitched research series over one underlying, and its roll history."""

    underlying_id: InstrumentId
    policy_revision: str
    bars: tuple[ContinuousBar, ...]
    rolls: tuple[FuturesRoll, ...]

    def __post_init__(self) -> None:
        """Require an ordered series whose contract changes exactly at its rolls."""
        invariant(bool(self.bars), "continuous series must not be empty")
        invariant(
            bool(_POLICY_REVISION.fullmatch(self.policy_revision)),
            "invalid roll policy revision",
        )
        dates = tuple(bar.trading_date for bar in self.bars)
        invariant(dates == tuple(sorted(dates)), "continuous series must be sorted")
        invariant(len(dates) == len(set(dates)), "continuous series dates must be unique")
        invariant(
            all(roll.policy_revision == self.policy_revision for roll in self.rolls),
            "a roll cannot belong to a different policy revision",
        )

        roll_dates = tuple(roll.roll_date for roll in self.rolls)
        invariant(roll_dates == tuple(sorted(set(roll_dates))), "rolls must be sorted and unique")
        by_date = {bar.trading_date: bar for bar in self.bars}
        for roll in self.rolls:
            invariant(roll.roll_date in by_date, "a roll date must be a session in the series")
            invariant(
                by_date[roll.roll_date].source_contract_id == roll.to_contract_id,
                "the session on a roll date must use the contract rolled to",
            )

        changes = tuple(
            current.trading_date
            for previous, current in zip(self.bars, self.bars[1:], strict=False)
            if previous.source_contract_id != current.source_contract_id
        )
        invariant(
            changes == roll_dates,
            "the source contract may change only on a recorded roll date",
        )

    def execution_contract_on(self, trading_date: date) -> ExecutionContract:
        """Resolve the actual contract a fill on ``trading_date`` must use.

        The single sanctioned bridge from research to simulated execution. It
        always answers with an actual contract and its then-effective lot size,
        never with the underlying or with the series itself.
        """
        for bar in self.bars:
            if bar.trading_date == trading_date:
                return ExecutionContract(
                    trading_date=trading_date,
                    contract_id=bar.source_contract_id,
                    instrument_token=bar.source_instrument_token,
                    expiry=bar.source_expiry,
                    lot_size=bar.source_lot_size,
                )
        raise MissingDataError(
            "continuous series has no session on the requested date",
            underlying_id=str(self.underlying_id),
            trading_date=trading_date.isoformat(),
        )


def _complete_candles(series: DailyBarSeries) -> dict[date, DailyCandle]:
    """Index one contract's settled sessions; incomplete bars cannot decide a roll."""
    return {
        bar.candle.trading_date: bar.candle
        for bar in series.bars
        if bar.completeness is BarCompleteness.COMPLETE
    }


def _is_more_liquid(candidate: DailyCandle, incumbent: DailyCandle) -> bool:
    """Decide liquidity dominance on one session, preferring open interest.

    Both volume and open interest must favour the candidate. One of the two
    crossing alone is an ordinary noisy session, and rolling on it produces a
    series whose contract changes depend on which day a scan happened to run.
    Open interest may be absent, in which case volume decides on its own --
    stated here rather than hidden behind a null-coalescing default.
    """
    if candidate.volume <= incumbent.volume:
        return False
    if candidate.open_interest is None or incumbent.open_interest is None:
        return True
    return candidate.open_interest > incumbent.open_interest


def build_continuous_series(
    *,
    underlying_id: InstrumentId,
    terms: tuple[FuturesContractTerms, ...],
    histories: dict[InstrumentId, DailyBarSeries],
    sessions: tuple[date, ...],
    policy: RollPolicy,
) -> ContinuousFuturesSeries:
    """Stitch actual-contract history into one research series, deterministically.

    ``sessions`` is the benchmark trading calendar, which decides what "two
    sessions before expiry" means. Counting calendar days instead would put the
    fallback roll on a holiday and make it depend on the weather.

    A roll is *decided* on one session and *materialised* on the next session the
    new contract actually prints. Materialising from the two bars that really
    abut the change -- rather than from the pair the decision was about -- is
    what makes the recorded provenance true even when the successor was itself
    illiquid or expired in between.
    """
    ordered = tuple(sorted(terms, key=lambda item: item.expiry))
    _validate_inputs(ordered, histories=histories, sessions=sessions)

    complete = {
        item.contract_id: _complete_candles(histories[item.contract_id]) for item in ordered
    }
    bars: list[ContinuousBar] = []
    rolls: list[FuturesRoll] = []
    index = 0
    effective_from: date | None = None
    # The reason *and* the contract it was about. A decision that named one
    # successor cannot explain a roll that landed on a different one -- which
    # happens when the named successor never printed and expired in the gap.
    pending: tuple[RollReason, InstrumentId] | None = None

    for position, session in enumerate(sessions):
        if effective_from is not None and session >= effective_from:
            index += 1
            effective_from = None
        index = _skip_expired(ordered, index=index, session=session)
        if index >= len(ordered):
            break
        front = ordered[index]
        candle = complete[front.contract_id].get(session)
        if candle is None:
            continue

        if bars and bars[-1].source_contract_id != front.contract_id:
            rolls.append(
                _materialise_roll(
                    previous=bars[-1],
                    front=front,
                    candle=candle,
                    reason=_reason_for(pending, front.contract_id),
                    policy_revision=policy.revision,
                )
            )
            pending = None

        bars.append(
            ContinuousBar(
                trading_date=session,
                candle=candle,
                source_contract_id=front.contract_id,
                source_instrument_token=front.instrument_token,
                source_expiry=front.expiry,
                source_lot_size=front.lot_size,
            )
        )

        if effective_from is not None or index + 1 >= len(ordered):
            continue
        if position + 1 >= len(sessions):
            continue
        reason = _decide_roll(
            front_candle=candle,
            successor_candle=complete[ordered[index + 1].contract_id].get(session),
            sessions_left=_sessions_until_expiry(sessions, position=position, expiry=front.expiry),
            policy=policy,
        )
        if reason is not None:
            pending = (reason, ordered[index + 1].contract_id)
            effective_from = sessions[position + 1]

    if not bars:
        raise MissingDataError(
            "no contract had a settled session in the requested range",
            underlying_id=str(underlying_id),
        )
    return ContinuousFuturesSeries(
        underlying_id=underlying_id,
        policy_revision=policy.revision,
        bars=tuple(bars),
        rolls=tuple(rolls),
    )


def _reason_for(
    pending: tuple[RollReason, InstrumentId] | None,
    landed_on: InstrumentId,
) -> RollReason:
    """Explain a roll only with a decision that was about the contract reached.

    No pending decision, or one naming a different successor, means the front
    contract simply ran out of life. Expiry is the floor no policy setting can
    push through, so that is what the record says.
    """
    if pending is not None and pending[1] == landed_on:
        return pending[0]
    return RollReason.EXPIRY_FALLBACK


def _materialise_roll(
    *,
    previous: ContinuousBar,
    front: FuturesContractTerms,
    candle: DailyCandle,
    reason: RollReason,
    policy_revision: str,
) -> FuturesRoll:
    """Record the switch from the last bar of one contract to the first of the next.

    The roll date is the new contract's own session, taken from the candle
    rather than passed alongside it, so the two cannot drift apart.
    """
    return FuturesRoll(
        decision_date=previous.trading_date,
        roll_date=candle.trading_date,
        reason=reason,
        from_contract_id=previous.source_contract_id,
        to_contract_id=front.contract_id,
        from_expiry=previous.source_expiry,
        to_expiry=front.expiry,
        from_close=previous.candle.close,
        to_close=candle.close,
        from_volume=previous.candle.volume,
        to_volume=candle.volume,
        from_open_interest=previous.candle.open_interest,
        to_open_interest=candle.open_interest,
        policy_revision=policy_revision,
    )


def _validate_inputs(
    ordered: tuple[FuturesContractTerms, ...],
    *,
    histories: dict[InstrumentId, DailyBarSeries],
    sessions: tuple[date, ...],
) -> None:
    """Refuse inputs that cannot produce a deterministic series."""
    if not ordered:
        raise ValidationError("a continuous series needs at least one contract")
    if not sessions:
        raise ValidationError("a continuous series needs a session calendar")
    if sessions != tuple(sorted(set(sessions))):
        raise ValidationError("the session calendar must be sorted and unique")
    expiries = tuple(item.expiry for item in ordered)
    if len(expiries) != len(set(expiries)):
        raise ValidationError("one underlying has one contract per expiry")
    missing = tuple(item.contract_id for item in ordered if item.contract_id not in histories)
    if missing:
        raise MissingDataError(
            "continuous series is missing a source contract's history",
            contracts=len(missing),
        )


def _skip_expired(
    ordered: tuple[FuturesContractTerms, ...],
    *,
    index: int,
    session: date,
) -> int:
    """Advance past any contract whose expiry has passed.

    Reached when liquidity never crossed and the buffer could not fire because
    the front contract simply stopped printing sessions. Expiry is the floor no
    policy setting can push through.
    """
    while index < len(ordered) and ordered[index].expiry < session:
        index += 1
    return index


def _sessions_until_expiry(sessions: tuple[date, ...], *, position: int, expiry: date) -> int:
    """Count settled calendar sessions from the one after ``position`` to expiry."""
    return sum(1 for item in sessions[position + 1 :] if item <= expiry)


def _decide_roll(
    *,
    front_candle: DailyCandle,
    successor_candle: DailyCandle | None,
    sessions_left: int,
    policy: RollPolicy,
) -> RollReason | None:
    """Apply the liquidity rule, then the expiry floor, to one decision session.

    Reads only this session's settled bars, and the caller applies the answer to
    the *next* session. A rule that switched contracts on the session it decided
    from would be trading a bar it had already seen the close of.
    """
    if successor_candle is None:
        return None
    if _is_more_liquid(successor_candle, front_candle):
        return RollReason.LIQUIDITY_CROSSOVER
    if sessions_left <= policy.expiry_buffer_sessions:
        return RollReason.EXPIRY_FALLBACK
    return None
