"""Future outcomes mature exactly and never use impossible signal-session execution."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    BenchmarkBasis,
    FutureOutcome,
    OutcomeStatus,
    TechnicalBar,
    TechnicalSeries,
    calculate_future_outcome,
)
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

SIGNAL = datetime(2026, 1, 2, 10, tzinfo=UTC)
STOCK = InstrumentId.deterministic("reference", "stock")
BENCHMARK = InstrumentId.deterministic("reference", "nifty")


def _series(  # noqa: PLR0913 - test builder exposes independent evidence conditions
    identity: InstrumentId,
    *,
    sessions: int,
    open_price: Decimal = Decimal(100),
    daily_gain: Decimal = Decimal(1),
    adjustment: AdjustmentEvidence = AdjustmentEvidence.UNKNOWN,
    missing: frozenset[int] = frozenset(),
) -> TechnicalSeries:
    start = SIGNAL.date() + timedelta(days=1)
    bars = tuple(
        TechnicalBar(
            trading_date=start + timedelta(days=index),
            open=open_price + daily_gain * Decimal(index),
            high=open_price + daily_gain * Decimal(index) + Decimal(2),
            low=open_price + daily_gain * Decimal(index) - Decimal(2),
            close=open_price + daily_gain * Decimal(index) + Decimal(1),
            volume=1_000,
        )
        for index in range(sessions)
        if index not in missing
    )
    return TechnicalSeries(instrument_id=identity, bars=bars, adjustment_status=adjustment)


def _outcome(*, stock_sessions: int, benchmark_sessions: int, horizon: int = 20) -> FutureOutcome:
    return calculate_future_outcome(
        stock=_series(STOCK, sessions=stock_sessions),
        benchmark=_series(BENCHMARK, sessions=benchmark_sessions),
        signal_cutoff=SIGNAL,
        observable_through=SIGNAL.date() + timedelta(days=benchmark_sessions),
        horizon_sessions=horizon,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        cost_bps=Decimal(20),
    )


def test_outcome_is_unavailable_before_exact_horizon_maturity() -> None:
    """N-1 observable future sessions cannot be silently shortened."""
    outcome = _outcome(stock_sessions=19, benchmark_sessions=19)

    assert outcome.status is OutcomeStatus.PENDING
    assert outcome.entry_date is None
    assert "20 future benchmark sessions are required; 19 observable" in outcome.limitation


def test_outcome_matures_on_nth_session_and_enters_at_next_open() -> None:
    """The signal close is never executable; session one opens after the signal."""
    outcome = _outcome(stock_sessions=20, benchmark_sessions=20)

    assert outcome.status is OutcomeStatus.MATURE
    assert outcome.entry_date == date(2026, 1, 3)
    assert outcome.entry_price == Decimal(100)
    assert outcome.exit_date == date(2026, 1, 22)
    assert outcome.exit_price == Decimal(120)
    assert outcome.absolute_return == Decimal("0.2")
    assert outcome.net_return == Decimal("0.198")
    assert outcome.benchmark_basis is BenchmarkBasis.PRICE_INDEX
    assert "UNKNOWN" in outcome.limitation


def test_missing_required_stock_session_fails_closed() -> None:
    """A mature benchmark cannot mask a missing stock exit-path session."""
    outcome = calculate_future_outcome(
        stock=_series(STOCK, sessions=20, missing=frozenset({19})),
        benchmark=_series(BENCHMARK, sessions=20),
        signal_cutoff=SIGNAL,
        observable_through=date(2026, 1, 22),
        horizon_sessions=20,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        cost_bps=Decimal(20),
    )

    assert outcome.status is OutcomeStatus.DATA_UNAVAILABLE
    assert "2026-01-22" in outcome.limitation


def test_missing_benchmark_and_raw_adjustment_are_explicit() -> None:
    """No benchmark or unsupported raw prices can produce a return claim."""
    missing = calculate_future_outcome(
        stock=_series(STOCK, sessions=20),
        benchmark=None,
        signal_cutoff=SIGNAL,
        observable_through=date(2026, 1, 22),
        horizon_sessions=20,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        cost_bps=Decimal(0),
    )
    raw = calculate_future_outcome(
        stock=_series(STOCK, sessions=20, adjustment=AdjustmentEvidence.RAW),
        benchmark=_series(BENCHMARK, sessions=20),
        signal_cutoff=SIGNAL,
        observable_through=date(2026, 1, 22),
        horizon_sessions=20,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        cost_bps=Decimal(0),
    )

    assert missing.status is OutcomeStatus.BENCHMARK_UNAVAILABLE
    assert raw.status is OutcomeStatus.UNSUPPORTED_ADJUSTMENT
