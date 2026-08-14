"""Exact formulas and availability boundaries for technical-features-v0."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    BenchmarkBasis,
    BenchmarkRegime,
    FeatureName,
    FeatureStatus,
    TechnicalBar,
    TechnicalFeatureSet,
    TechnicalSeries,
    compute_technical_features,
)
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

STOCK = InstrumentId.deterministic("reference", "stock")
BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")


def _series(
    instrument_id: InstrumentId,
    closes: tuple[Decimal, ...],
    *,
    start: date = date(2025, 1, 1),
    volumes: tuple[int, ...] | None = None,
    adjustment: AdjustmentEvidence = AdjustmentEvidence.UNKNOWN,
) -> TechnicalSeries:
    active_volumes = volumes or tuple(1000 for _ in closes)
    return TechnicalSeries(
        instrument_id=instrument_id,
        adjustment_status=adjustment,
        bars=tuple(
            TechnicalBar(
                trading_date=start + timedelta(days=index),
                open=close,
                high=close + Decimal(2),
                low=close - Decimal(1),
                close=close,
                volume=active_volumes[index],
            )
            for index, close in enumerate(closes)
        ),
    )


def _features(
    stock: TechnicalSeries,
    benchmark: TechnicalSeries | None = None,
) -> TechnicalFeatureSet:
    latest = stock.bars[-1].trading_date
    return compute_technical_features(
        stock=stock,
        benchmark=benchmark,
        cutoff=datetime.combine(latest + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
    )


def test_total_returns_use_exact_backward_session_windows() -> None:
    """Returns use the close exactly N sessions behind the cutoff close."""
    closes = tuple(Decimal(100 + index) for index in range(130))
    features = _features(_series(STOCK, closes), _series(BENCHMARK, closes))

    for sessions, name in (
        (5, FeatureName.RETURN_5),
        (20, FeatureName.RETURN_20),
        (60, FeatureName.RETURN_60),
        (120, FeatureName.RETURN_120),
    ):
        expected = closes[-1] / closes[-(sessions + 1)] - Decimal(1)
        assert features.get(name).value == expected


def test_cutoff_excludes_future_stock_and_benchmark_bars() -> None:
    """A wider caller series cannot leak a post-cutoff close into any feature."""
    closes = tuple(Decimal(100 + index) for index in range(131))
    stock = _series(STOCK, closes)
    benchmark = _series(BENCHMARK, closes)
    cutoff_date = stock.bars[-2].trading_date

    features = compute_technical_features(
        stock=stock,
        benchmark=benchmark,
        cutoff=datetime.combine(cutoff_date, datetime.max.time(), tzinfo=UTC),
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
    )

    assert features.latest_session == cutoff_date
    assert features.get(FeatureName.RETURN_5).value == closes[-2] / closes[-7] - Decimal(1)


@pytest.mark.parametrize(
    ("name", "warmup"),
    [
        (FeatureName.RETURN_5, 6),
        (FeatureName.RETURN_20, 21),
        (FeatureName.RETURN_60, 61),
        (FeatureName.RETURN_120, 121),
        (FeatureName.CLOSE_VS_MA50, 50),
        (FeatureName.MA50_VS_MA200, 200),
        (FeatureName.REALIZED_VOL_60, 61),
        (FeatureName.DRAWDOWN_126, 126),
        (FeatureName.ATR14_NORMALIZED, 15),
        (FeatureName.VOLUME_RATIO_20_MEDIAN, 20),
    ],
)
def test_warmup_boundaries_are_exact(name: FeatureName, warmup: int) -> None:
    """Each rolling feature becomes available at its declared warm-up boundary."""
    short = _series(STOCK, tuple(Decimal(100 + index) for index in range(warmup - 1)))
    ready = _series(STOCK, tuple(Decimal(100 + index) for index in range(warmup)))

    assert _features(short).get(name).status is FeatureStatus.INSUFFICIENT_HISTORY
    assert _features(ready).get(name).status is FeatureStatus.AVAILABLE


def test_benchmark_relative_returns_use_aligned_price_index_sessions() -> None:
    """Excess returns subtract aligned price-index returns over the same window."""
    stock = _series(STOCK, tuple(Decimal(100 + index * 2) for index in range(130)))
    benchmark = _series(BENCHMARK, tuple(Decimal(100 + index) for index in range(130)))
    features = _features(stock, benchmark)

    expected = (
        stock.bars[-1].close / stock.bars[-121].close
        - benchmark.bars[-1].close / benchmark.bars[-121].close
    )
    excess = features.get(FeatureName.EXCESS_RETURN_120)
    assert excess.status is FeatureStatus.AVAILABLE
    assert excess.value == expected
    assert features.benchmark_basis is BenchmarkBasis.PRICE_INDEX


def test_unavailable_or_misaligned_benchmark_is_never_zero_filled() -> None:
    """Missing benchmark coverage produces an explicit unavailable state."""
    stock = _series(STOCK, tuple(Decimal(100 + index) for index in range(130)))
    shifted = _series(
        BENCHMARK,
        tuple(Decimal(100 + index) for index in range(130)),
        start=date(2025, 1, 2),
    )

    absent = _features(stock).get(FeatureName.EXCESS_RETURN_120)
    misaligned = _features(stock, shifted).get(FeatureName.EXCESS_RETURN_120)

    assert absent.status is FeatureStatus.BENCHMARK_UNAVAILABLE
    assert absent.value is None
    assert misaligned.status is FeatureStatus.BENCHMARK_UNAVAILABLE
    assert misaligned.value is None


def test_trend_drawdown_atr_volume_and_turnover_formulas_are_exact() -> None:
    """Trend, range, drawdown, participation, and liquidity formulas stay frozen."""
    closes = tuple(Decimal(100 + index) for index in range(200))
    volumes = tuple(1000 + index for index in range(200))
    features = _features(
        _series(STOCK, closes, volumes=volumes),
        _series(BENCHMARK, closes),
    )

    ma50 = sum(closes[-50:], Decimal(0)) / Decimal(50)
    ma200 = sum(closes, Decimal(0)) / Decimal(200)
    assert features.get(FeatureName.CLOSE_VS_MA50).value == closes[-1] / ma50 - 1
    assert features.get(FeatureName.MA50_VS_MA200).value == ma50 / ma200 - 1
    assert features.get(FeatureName.DRAWDOWN_126).value == Decimal(0)
    assert features.get(FeatureName.ATR14_NORMALIZED).value == Decimal(3) / closes[-1]

    trailing_volumes = tuple(Decimal(item) for item in volumes[-20:])
    median_volume = (trailing_volumes[9] + trailing_volumes[10]) / Decimal(2)
    assert (
        features.get(FeatureName.VOLUME_RATIO_20_MEDIAN).value
        == trailing_volumes[-1] / median_volume
    )
    turnover = sorted(
        close * Decimal(volume) for close, volume in zip(closes[-20:], volumes[-20:], strict=True)
    )
    expected_turnover = (turnover[9] + turnover[10]) / Decimal(2)
    assert features.get(FeatureName.MEDIAN_RUPEE_TURNOVER_20).value == expected_turnover


def test_realized_and_downside_volatility_use_252_session_annualization() -> None:
    """Volatility uses population moments and a square-root-252 annualizer."""
    returns = (Decimal("0.01"), Decimal("-0.02"), Decimal("0.03")) * 20
    closes = [Decimal(100)]
    for item in returns:
        closes.append(closes[-1] * (Decimal(1) + item))
    stock = _series(STOCK, tuple(closes))
    features = _features(stock)
    mean = sum(returns, Decimal(0)) / Decimal(60)
    variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(60)
    downside_variance = sum((min(item, Decimal(0)) ** 2 for item in returns), Decimal(0)) / Decimal(
        60
    )
    with localcontext() as context:
        context.prec = 28
        expected = variance.sqrt() * Decimal(252).sqrt()
        expected_downside = downside_variance.sqrt() * Decimal(252).sqrt()

    assert features.get(FeatureName.REALIZED_VOL_60).value == pytest.approx(expected)
    assert features.get(FeatureName.DOWNSIDE_VOL_60).value == pytest.approx(expected_downside)


def test_regime_is_deterministic_and_adjustment_limitation_is_explicit() -> None:
    """Benchmark regime and unverified-adjustment limitations are deterministic."""
    rising = tuple(Decimal(100 + index) for index in range(220))
    falling = tuple(Decimal(400 - index) for index in range(220))

    risk_on = _features(_series(STOCK, rising), _series(BENCHMARK, rising))
    risk_off = _features(_series(STOCK, rising), _series(BENCHMARK, falling))

    assert risk_on.benchmark_regime is BenchmarkRegime.RISK_ON
    assert risk_off.benchmark_regime is BenchmarkRegime.RISK_OFF
    assert "corporate-action adjustment status is UNKNOWN" in risk_on.limitations
    assert "benchmark is a price index; dividends are excluded" in risk_on.limitations


def test_stale_series_blocks_every_feature() -> None:
    """A stale latest observation blocks every stock feature."""
    stock = _series(STOCK, tuple(Decimal(100 + index) for index in range(130)))
    result = compute_technical_features(
        stock=stock,
        benchmark=None,
        cutoff=datetime(2026, 12, 1, tzinfo=UTC),
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
    )

    assert {item.status for item in result.features} == {FeatureStatus.STALE}
    assert all(item.value is None for item in result.features)
