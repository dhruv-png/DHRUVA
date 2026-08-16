"""Deterministic future-outcome arithmetic for research evaluation.

The calculator consumes already-resolved series.  It never queries persistence,
chooses a universe, or changes a frozen ranking.  The first tradable price is
the next observed session's open; the exit is the close of holding session N.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Final, TypedDict

from dhruva.contexts.analytics.domain.technical import (
    TRADING_SESSIONS_PER_YEAR,
    AdjustmentEvidence,
    BenchmarkBasis,
    TechnicalBar,
    TechnicalSeries,
)
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "CANDIDATE_OUTCOME_REVISION",
    "DEFAULT_EVALUATION_HORIZONS",
    "FutureOutcome",
    "OutcomeStatus",
    "calculate_future_outcome",
]

CANDIDATE_OUTCOME_REVISION: Final = "candidate-outcome-v0"
DEFAULT_EVALUATION_HORIZONS: Final = (20, 60)
_BPS_DENOMINATOR = Decimal(10_000)


class _OutcomeCommon(TypedDict):
    instrument_id: InstrumentId
    signal_cutoff: datetime
    observable_through: date
    horizon_sessions: int
    benchmark_symbol: str
    benchmark_basis: BenchmarkBasis
    cost_bps: Decimal
    adjustment_status: AdjustmentEvidence


class OutcomeStatus(StrEnum):
    """Whether an outcome is observable and defensible at the supplied cutoff."""

    PENDING = "PENDING"
    MATURE = "MATURE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    BENCHMARK_UNAVAILABLE = "BENCHMARK_UNAVAILABLE"
    UNSUPPORTED_ADJUSTMENT = "UNSUPPORTED_ADJUSTMENT"


@dataclass(frozen=True, slots=True)
class FutureOutcome:
    """One horizon's immutable realized paper-research fact."""

    instrument_id: InstrumentId
    signal_cutoff: datetime
    observable_through: date
    horizon_sessions: int
    status: OutcomeStatus
    benchmark_symbol: str
    benchmark_basis: BenchmarkBasis
    cost_bps: Decimal
    entry_date: date | None = None
    entry_price: Decimal | None = None
    exit_date: date | None = None
    exit_price: Decimal | None = None
    absolute_return: Decimal | None = None
    benchmark_return: Decimal | None = None
    excess_return: Decimal | None = None
    net_return: Decimal | None = None
    net_excess_return: Decimal | None = None
    maximum_adverse_excursion: Decimal | None = None
    maximum_favorable_excursion: Decimal | None = None
    holding_period_drawdown: Decimal | None = None
    realized_volatility: Decimal | None = None
    adjustment_status: AdjustmentEvidence | None = None
    limitation: str = ""
    revision: str = CANDIDATE_OUTCOME_REVISION

    def __post_init__(self) -> None:
        """Keep mature and unavailable states unambiguous."""
        invariant(self.horizon_sessions > 0, "outcome horizon must be positive")
        invariant(self.cost_bps >= 0, "research cost cannot be negative")
        values = (
            self.entry_date,
            self.entry_price,
            self.exit_date,
            self.exit_price,
            self.absolute_return,
            self.benchmark_return,
            self.excess_return,
            self.net_return,
            self.net_excess_return,
            self.maximum_adverse_excursion,
            self.maximum_favorable_excursion,
            self.holding_period_drawdown,
            self.realized_volatility,
        )
        mature = self.status is OutcomeStatus.MATURE
        invariant(mature == all(value is not None for value in values), "outcome state incomplete")
        invariant(
            mature or all(value is None for value in values),
            "unavailable outcome has values",
        )
        invariant(mature or self.limitation.strip() != "", "unavailable outcome needs a reason")


def calculate_future_outcome(  # noqa: PLR0913 - methodology inputs must remain explicit
    *,
    stock: TechnicalSeries,
    benchmark: TechnicalSeries | None,
    signal_cutoff: datetime,
    observable_through: date,
    horizon_sessions: int,
    benchmark_basis: BenchmarkBasis,
    cost_bps: Decimal,
    benchmark_symbol: str = "NIFTY 50",
) -> FutureOutcome:
    """Calculate a next-session-open to Nth-session-close research outcome.

    Benchmark sessions define maturity.  A stock must contain the exact same
    entry-through-exit dates; the calculator never shortens the horizon.
    """
    invariant(horizon_sessions > 0, "outcome horizon must be positive")
    invariant(cost_bps >= 0, "research cost cannot be negative")
    common: _OutcomeCommon = {
        "instrument_id": stock.instrument_id,
        "signal_cutoff": signal_cutoff,
        "observable_through": observable_through,
        "horizon_sessions": horizon_sessions,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_basis": benchmark_basis,
        "cost_bps": cost_bps,
        "adjustment_status": stock.adjustment_status,
    }
    if stock.adjustment_status is AdjustmentEvidence.RAW:
        return FutureOutcome(
            **common,
            status=OutcomeStatus.UNSUPPORTED_ADJUSTMENT,
            limitation="raw prices have no supported corporate-action adjustment evidence",
        )
    if benchmark is None:
        return FutureOutcome(
            **common,
            status=OutcomeStatus.BENCHMARK_UNAVAILABLE,
            limitation="NIFTY 50 PRICE_INDEX history is unavailable",
        )

    benchmark_future = tuple(
        bar
        for bar in benchmark.bars
        if signal_cutoff.date() < bar.trading_date <= observable_through
    )
    if len(benchmark_future) < horizon_sessions:
        return FutureOutcome(
            **common,
            status=OutcomeStatus.PENDING,
            limitation=(
                f"{horizon_sessions} future benchmark sessions are required; "
                f"{len(benchmark_future)} observable"
            ),
        )
    benchmark_window = benchmark_future[:horizon_sessions]
    required_dates = tuple(bar.trading_date for bar in benchmark_window)
    stock_by_date = {
        bar.trading_date: bar
        for bar in stock.bars
        if signal_cutoff.date() < bar.trading_date <= observable_through
    }
    if any(day not in stock_by_date for day in required_dates):
        missing = next(day for day in required_dates if day not in stock_by_date)
        return FutureOutcome(
            **common,
            status=OutcomeStatus.DATA_UNAVAILABLE,
            limitation=f"stock history is missing required session {missing.isoformat()}",
        )
    stock_window = tuple(stock_by_date[day] for day in required_dates)
    entry_price = stock_window[0].open
    exit_price = stock_window[-1].close
    benchmark_entry = benchmark_window[0].open
    benchmark_exit = benchmark_window[-1].close
    absolute = exit_price / entry_price - Decimal(1)
    benchmark_return = benchmark_exit / benchmark_entry - Decimal(1)
    excess = absolute - benchmark_return
    cost = cost_bps / _BPS_DENOMINATOR
    net = absolute - cost
    returns = _holding_returns(stock_window)
    limitation = (
        "corporate-action adjustment semantics are UNKNOWN; outcome is diagnostic only"
        if stock.adjustment_status is AdjustmentEvidence.UNKNOWN
        else ""
    )
    return FutureOutcome(
        **common,
        status=OutcomeStatus.MATURE,
        entry_date=stock_window[0].trading_date,
        entry_price=entry_price,
        exit_date=stock_window[-1].trading_date,
        exit_price=exit_price,
        absolute_return=absolute,
        benchmark_return=benchmark_return,
        excess_return=excess,
        net_return=net,
        net_excess_return=net - benchmark_return,
        maximum_adverse_excursion=min(bar.low / entry_price - Decimal(1) for bar in stock_window),
        maximum_favorable_excursion=max(
            bar.high / entry_price - Decimal(1) for bar in stock_window
        ),
        holding_period_drawdown=_holding_drawdown(stock_window, entry_price=entry_price),
        realized_volatility=_realized_volatility(returns),
        limitation=limitation,
    )


def _holding_returns(bars: tuple[TechnicalBar, ...]) -> tuple[Decimal, ...]:
    closes = (bars[0].open, *(bar.close for bar in bars))
    return tuple(current / previous - Decimal(1) for previous, current in pairwise(closes))


def _realized_volatility(returns: tuple[Decimal, ...]) -> Decimal:
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(len(returns))
    with localcontext() as context:
        context.prec = 28
        return variance.sqrt() * TRADING_SESSIONS_PER_YEAR.sqrt()


def _holding_drawdown(bars: tuple[TechnicalBar, ...], *, entry_price: Decimal) -> Decimal:
    peak = entry_price
    worst = Decimal(0)
    for bar in bars:
        worst = min(worst, bar.low / peak - Decimal(1))
        peak = max(peak, bar.high)
    return worst
