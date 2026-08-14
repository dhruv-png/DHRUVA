"""Point-in-time technical features over already-resolved daily bar series."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Final

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "TECHNICAL_FEATURE_REVISION",
    "AdjustmentEvidence",
    "BenchmarkBasis",
    "BenchmarkRegime",
    "FeatureName",
    "FeatureStatus",
    "TechnicalBar",
    "TechnicalFeature",
    "TechnicalFeatureSet",
    "TechnicalSeries",
    "compute_technical_features",
    "unavailable_technical_features",
]

TECHNICAL_FEATURE_REVISION: Final = "technical-features-v0"
TRADING_SESSIONS_PER_YEAR: Final = Decimal(252)
_STALE_AFTER_CALENDAR_DAYS = 7
_REGIME_TREND_SESSIONS = 200
_ALL_FEATURES = (
    "RETURN_5",
    "RETURN_20",
    "RETURN_60",
    "RETURN_120",
    "EXCESS_RETURN_20",
    "EXCESS_RETURN_60",
    "EXCESS_RETURN_120",
    "CLOSE_VS_MA50",
    "MA50_VS_MA200",
    "REALIZED_VOL_20",
    "REALIZED_VOL_60",
    "DOWNSIDE_VOL_60",
    "DRAWDOWN_126",
    "ATR14_NORMALIZED",
    "VOLUME_RATIO_20_MEDIAN",
    "MEDIAN_RUPEE_TURNOVER_20",
)


class BenchmarkBasis(StrEnum):
    """Return basis carried beside every benchmark-relative fact."""

    PRICE_INDEX = "PRICE_INDEX"
    TOTAL_RETURN_INDEX = "TOTAL_RETURN_INDEX"


class AdjustmentEvidence(StrEnum):
    """Provider-neutral proof level for corporate-action adjustment."""

    RAW = "RAW"
    ADJUSTED = "ADJUSTED"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class FeatureStatus(StrEnum):
    """Why a technical fact does or does not have a value."""

    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    BENCHMARK_UNAVAILABLE = "BENCHMARK_UNAVAILABLE"
    STALE = "STALE"
    UNSUPPORTED_ADJUSTMENT = "UNSUPPORTED_ADJUSTMENT"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class FeatureName(StrEnum):
    """The compact v0 feature vocabulary; no outcome or forward-return field."""

    RETURN_5 = "RETURN_5"
    RETURN_20 = "RETURN_20"
    RETURN_60 = "RETURN_60"
    RETURN_120 = "RETURN_120"
    EXCESS_RETURN_20 = "EXCESS_RETURN_20"
    EXCESS_RETURN_60 = "EXCESS_RETURN_60"
    EXCESS_RETURN_120 = "EXCESS_RETURN_120"
    CLOSE_VS_MA50 = "CLOSE_VS_MA50"
    MA50_VS_MA200 = "MA50_VS_MA200"
    REALIZED_VOL_20 = "REALIZED_VOL_20"
    REALIZED_VOL_60 = "REALIZED_VOL_60"
    DOWNSIDE_VOL_60 = "DOWNSIDE_VOL_60"
    DRAWDOWN_126 = "DRAWDOWN_126"
    ATR14_NORMALIZED = "ATR14_NORMALIZED"
    VOLUME_RATIO_20_MEDIAN = "VOLUME_RATIO_20_MEDIAN"
    MEDIAN_RUPEE_TURNOVER_20 = "MEDIAN_RUPEE_TURNOVER_20"


class BenchmarkRegime(StrEnum):
    """Transparent price-index regime from trend and short/medium volatility."""

    RISK_ON = "RISK_ON"
    MIXED = "MIXED"
    RISK_OFF = "RISK_OFF"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class TechnicalBar:
    """Primitive daily observation consumed by technical arithmetic."""

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class TechnicalSeries:
    """One compatible instrument series already resolved at a PIT cutoff."""

    instrument_id: InstrumentId
    bars: tuple[TechnicalBar, ...]
    adjustment_status: AdjustmentEvidence

    def __post_init__(self) -> None:
        """Require a sorted, unique, non-empty series."""
        invariant(bool(self.bars), "technical series must contain bars")
        dates = tuple(item.trading_date for item in self.bars)
        invariant(dates == tuple(sorted(dates)), "technical series must be sorted")
        invariant(len(set(dates)) == len(dates), "technical series dates must be unique")


@dataclass(frozen=True, slots=True)
class TechnicalFeature:
    """One feature value or an explicit reason it cannot be computed."""

    name: FeatureName
    status: FeatureStatus
    required_sessions: int
    value: Decimal | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        """Require value and status to describe the same availability state."""
        invariant(self.required_sessions >= 1, "feature warm-up must be positive")
        invariant(
            (self.status is FeatureStatus.AVAILABLE) == (self.value is not None),
            "feature availability contradicts its value",
        )
        if self.value is not None:
            invariant(self.value.is_finite(), "technical feature must be finite")
        else:
            invariant(self.reason.strip() != "", "unavailable feature must say why")


@dataclass(frozen=True, slots=True)
class TechnicalFeatureSet:
    """All v0 facts for one instrument at one knowledge cutoff."""

    instrument_id: InstrumentId
    cutoff: datetime
    latest_session: date | None
    adjustment_status: AdjustmentEvidence | None
    features: tuple[TechnicalFeature, ...]
    benchmark_basis: BenchmarkBasis
    benchmark_regime: BenchmarkRegime
    benchmark_regime_status: FeatureStatus
    limitations: tuple[str, ...]
    revision: str = TECHNICAL_FEATURE_REVISION

    def __post_init__(self) -> None:
        """Require the complete fixed vocabulary and unique limitations."""
        names = tuple(item.name for item in self.features)
        invariant(names == tuple(FeatureName(item) for item in _ALL_FEATURES), "feature set drift")
        invariant(len(set(names)) == len(names), "technical feature appears twice")
        invariant(len(set(self.limitations)) == len(self.limitations), "limitation repeated")

    def get(self, name: FeatureName) -> TechnicalFeature:
        """Return the named feature from the fixed complete vocabulary."""
        return next(item for item in self.features if item.name is name)


def unavailable_technical_features(
    *,
    instrument_id: InstrumentId,
    cutoff: datetime,
    status: FeatureStatus,
    reason: str,
    benchmark_basis: BenchmarkBasis = BenchmarkBasis.PRICE_INDEX,
) -> TechnicalFeatureSet:
    """Build a complete no-value feature set; absence is never encoded as zero."""
    invariant(status is not FeatureStatus.AVAILABLE, "unavailable set needs unavailable status")
    return TechnicalFeatureSet(
        instrument_id=instrument_id,
        cutoff=cutoff,
        latest_session=None,
        adjustment_status=None,
        features=tuple(
            TechnicalFeature(
                name=name,
                status=status,
                required_sessions=_warmup(name),
                reason=reason,
            )
            for name in FeatureName
        ),
        benchmark_basis=benchmark_basis,
        benchmark_regime=BenchmarkRegime.UNAVAILABLE,
        benchmark_regime_status=status,
        limitations=(reason,),
    )


def compute_technical_features(
    *,
    stock: TechnicalSeries,
    benchmark: TechnicalSeries | None,
    cutoff: datetime,
    benchmark_basis: BenchmarkBasis,
) -> TechnicalFeatureSet:
    """Compute backward-looking features over PIT-resolved input series only."""
    bars = tuple(item for item in stock.bars if item.trading_date <= cutoff.date())
    instrument_id = stock.instrument_id
    if not bars:
        return unavailable_technical_features(
            instrument_id=instrument_id,
            cutoff=cutoff,
            status=FeatureStatus.DATA_UNAVAILABLE,
            reason="no stock bar is visible at the cutoff",
            benchmark_basis=benchmark_basis,
        )
    benchmark = _series_at_cutoff(benchmark, cutoff=cutoff)
    latest = bars[-1].trading_date
    limitations: list[str] = []
    if stock.adjustment_status.value in {"UNKNOWN", "RAW"}:
        limitations.append(f"corporate-action adjustment status is {stock.adjustment_status.value}")
    if benchmark_basis is BenchmarkBasis.PRICE_INDEX:
        limitations.append("benchmark is a price index; dividends are excluded")

    if (cutoff.date() - latest).days > _STALE_AFTER_CALENDAR_DAYS:
        reason = f"latest session {latest.isoformat()} is stale at cutoff"
        return TechnicalFeatureSet(
            instrument_id=instrument_id,
            cutoff=cutoff,
            latest_session=latest,
            adjustment_status=stock.adjustment_status,
            features=tuple(
                TechnicalFeature(
                    name=name,
                    status=FeatureStatus.STALE,
                    required_sessions=_warmup(name),
                    reason=reason,
                )
                for name in FeatureName
            ),
            benchmark_basis=benchmark_basis,
            benchmark_regime=BenchmarkRegime.UNAVAILABLE,
            benchmark_regime_status=FeatureStatus.STALE,
            limitations=(*limitations, reason),
        )

    if stock.adjustment_status is AdjustmentEvidence.RAW:
        reason = "raw history has no supported corporate-action adjustment evidence"
        return TechnicalFeatureSet(
            instrument_id=instrument_id,
            cutoff=cutoff,
            latest_session=latest,
            adjustment_status=stock.adjustment_status,
            features=tuple(
                TechnicalFeature(
                    name=name,
                    status=FeatureStatus.UNSUPPORTED_ADJUSTMENT,
                    required_sessions=_warmup(name),
                    reason=reason,
                )
                for name in FeatureName
            ),
            benchmark_basis=benchmark_basis,
            benchmark_regime=BenchmarkRegime.UNAVAILABLE,
            benchmark_regime_status=FeatureStatus.UNSUPPORTED_ADJUSTMENT,
            limitations=(*limitations, reason),
        )

    absolute = {
        FeatureName.RETURN_5: _total_return(bars, 5),
        FeatureName.RETURN_20: _total_return(bars, 20),
        FeatureName.RETURN_60: _total_return(bars, 60),
        FeatureName.RETURN_120: _total_return(bars, 120),
        FeatureName.CLOSE_VS_MA50: _close_vs_average(bars, 50),
        FeatureName.MA50_VS_MA200: _average_relationship(bars, 50, 200),
        FeatureName.REALIZED_VOL_20: _realized_volatility(bars, 20),
        FeatureName.REALIZED_VOL_60: _realized_volatility(bars, 60),
        FeatureName.DOWNSIDE_VOL_60: _downside_volatility(bars, 60),
        FeatureName.DRAWDOWN_126: _drawdown(bars, 126),
        FeatureName.ATR14_NORMALIZED: _normalized_atr(bars, 14),
        FeatureName.VOLUME_RATIO_20_MEDIAN: _volume_ratio(bars, 20),
        FeatureName.MEDIAN_RUPEE_TURNOVER_20: _median_turnover(bars, 20),
    }
    features: list[TechnicalFeature] = []
    for name in FeatureName:
        if name in {
            FeatureName.EXCESS_RETURN_20,
            FeatureName.EXCESS_RETURN_60,
            FeatureName.EXCESS_RETURN_120,
        }:
            sessions = int(name.value.rsplit("_", 1)[1])
            result, status, reason = _excess_return(bars, benchmark, sessions)
        else:
            result = absolute[name]
            status = (
                FeatureStatus.AVAILABLE
                if result is not None
                else FeatureStatus.INSUFFICIENT_HISTORY
            )
            reason = "" if result is not None else _history_reason(name, len(bars))
        features.append(
            TechnicalFeature(
                name=name,
                status=status,
                required_sessions=_warmup(name),
                value=result,
                reason=reason,
            )
        )

    regime, regime_status = _benchmark_regime(benchmark, cutoff=cutoff)
    return TechnicalFeatureSet(
        instrument_id=instrument_id,
        cutoff=cutoff,
        latest_session=latest,
        adjustment_status=stock.adjustment_status,
        features=tuple(features),
        benchmark_basis=benchmark_basis,
        benchmark_regime=regime,
        benchmark_regime_status=regime_status,
        limitations=tuple(limitations),
    )


def _series_at_cutoff(
    series: TechnicalSeries | None, *, cutoff: datetime
) -> TechnicalSeries | None:
    """Exclude future sessions even if a caller supplies a wider resolved series."""
    if series is None:
        return None
    bars = tuple(item for item in series.bars if item.trading_date <= cutoff.date())
    if not bars:
        return None
    return TechnicalSeries(
        instrument_id=series.instrument_id,
        bars=bars,
        adjustment_status=series.adjustment_status,
    )


def _warmup(name: FeatureName) -> int:
    return {
        FeatureName.RETURN_5: 6,
        FeatureName.RETURN_20: 21,
        FeatureName.RETURN_60: 61,
        FeatureName.RETURN_120: 121,
        FeatureName.EXCESS_RETURN_20: 21,
        FeatureName.EXCESS_RETURN_60: 61,
        FeatureName.EXCESS_RETURN_120: 121,
        FeatureName.CLOSE_VS_MA50: 50,
        FeatureName.MA50_VS_MA200: 200,
        FeatureName.REALIZED_VOL_20: 21,
        FeatureName.REALIZED_VOL_60: 61,
        FeatureName.DOWNSIDE_VOL_60: 61,
        FeatureName.DRAWDOWN_126: 126,
        FeatureName.ATR14_NORMALIZED: 15,
        FeatureName.VOLUME_RATIO_20_MEDIAN: 20,
        FeatureName.MEDIAN_RUPEE_TURNOVER_20: 20,
    }[name]


def _history_reason(name: FeatureName, available: int) -> str:
    return f"{name.value} requires {_warmup(name)} sessions; {available} available"


def _total_return(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions + 1:
        return None
    return bars[-1].close / bars[-(sessions + 1)].close - Decimal(1)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _close_vs_average(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions:
        return None
    average = _mean([item.close for item in bars[-sessions:]])
    return bars[-1].close / average - Decimal(1)


def _average_relationship(bars: tuple[TechnicalBar, ...], short: int, long: int) -> Decimal | None:
    if len(bars) < long:
        return None
    short_average = _mean([item.close for item in bars[-short:]])
    long_average = _mean([item.close for item in bars[-long:]])
    return short_average / long_average - Decimal(1)


def _returns(bars: tuple[TechnicalBar, ...], sessions: int) -> list[Decimal] | None:
    if len(bars) < sessions + 1:
        return None
    closes = [item.close for item in bars[-(sessions + 1) :]]
    return [current / previous - Decimal(1) for previous, current in pairwise(closes)]


def _realized_volatility(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    returns = _returns(bars, sessions)
    if returns is None:
        return None
    mean = _mean(returns)
    variance = _mean([(item - mean) ** 2 for item in returns])
    with localcontext() as context:
        context.prec = 28
        return variance.sqrt() * TRADING_SESSIONS_PER_YEAR.sqrt()


def _downside_volatility(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    returns = _returns(bars, sessions)
    if returns is None:
        return None
    downside = [min(item, Decimal(0)) ** 2 for item in returns]
    with localcontext() as context:
        context.prec = 28
        return _mean(downside).sqrt() * TRADING_SESSIONS_PER_YEAR.sqrt()


def _drawdown(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions:
        return None
    closes = [item.close for item in bars[-sessions:]]
    return closes[-1] / max(closes) - Decimal(1)


def _normalized_atr(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions + 1:
        return None
    selected = bars[-(sessions + 1) :]
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in pairwise(selected)
    ]
    return _mean(ranges) / bars[-1].close


def _volume_ratio(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions:
        return None
    volumes = [Decimal(item.volume) for item in bars[-sessions:]]
    median = _median(volumes)
    return None if median == 0 else volumes[-1] / median


def _median_turnover(bars: tuple[TechnicalBar, ...], sessions: int) -> Decimal | None:
    if len(bars) < sessions:
        return None
    return _median([item.close * Decimal(item.volume) for item in bars[-sessions:]])


def _excess_return(
    stock: tuple[TechnicalBar, ...], benchmark: TechnicalSeries | None, sessions: int
) -> tuple[Decimal | None, FeatureStatus, str]:
    if len(stock) < sessions + 1:
        name = FeatureName(f"EXCESS_RETURN_{sessions}")
        return None, FeatureStatus.INSUFFICIENT_HISTORY, _history_reason(name, len(stock))
    if benchmark is None or len(benchmark.bars) < sessions + 1:
        return None, FeatureStatus.BENCHMARK_UNAVAILABLE, "benchmark window is unavailable"
    stock_window = stock[-(sessions + 1) :]
    benchmark_window = benchmark.bars[-(sessions + 1) :]
    stock_dates = tuple(item.trading_date for item in stock_window)
    benchmark_dates = tuple(item.trading_date for item in benchmark_window)
    if stock_dates != benchmark_dates:
        return None, FeatureStatus.BENCHMARK_UNAVAILABLE, "benchmark sessions do not align"
    stock_return = stock_window[-1].close / stock_window[0].close - Decimal(1)
    benchmark_return = benchmark_window[-1].close / benchmark_window[0].close - Decimal(1)
    return stock_return - benchmark_return, FeatureStatus.AVAILABLE, ""


def _benchmark_regime(  # noqa: PLR0911 - each refusal state is intentionally explicit
    benchmark: TechnicalSeries | None, *, cutoff: datetime
) -> tuple[BenchmarkRegime, FeatureStatus]:
    if benchmark is None:
        return BenchmarkRegime.UNAVAILABLE, FeatureStatus.BENCHMARK_UNAVAILABLE
    if (cutoff.date() - benchmark.bars[-1].trading_date).days > _STALE_AFTER_CALENDAR_DAYS:
        return BenchmarkRegime.UNAVAILABLE, FeatureStatus.STALE
    if len(benchmark.bars) < _REGIME_TREND_SESSIONS:
        return BenchmarkRegime.UNAVAILABLE, FeatureStatus.INSUFFICIENT_HISTORY
    close_vs_ma = _close_vs_average(benchmark.bars, _REGIME_TREND_SESSIONS)
    vol20 = _realized_volatility(benchmark.bars, 20)
    vol60 = _realized_volatility(benchmark.bars, 60)
    if close_vs_ma is None or vol20 is None or vol60 is None:  # defensive totality
        return BenchmarkRegime.UNAVAILABLE, FeatureStatus.INSUFFICIENT_HISTORY
    if close_vs_ma < 0 or vol20 > vol60 * Decimal("1.25"):
        return BenchmarkRegime.RISK_OFF, FeatureStatus.AVAILABLE
    if vol20 <= vol60:
        return BenchmarkRegime.RISK_ON, FeatureStatus.AVAILABLE
    return BenchmarkRegime.MIXED, FeatureStatus.AVAILABLE
