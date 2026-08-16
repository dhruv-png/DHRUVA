"""Assemble analytics facts into the frozen deterministic candidate baseline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

from dhruva.contexts.analytics.api import (
    TECHNICAL_FEATURE_REVISION,
    AdjustmentEvidence,
    BenchmarkBasis,
    BenchmarkRegime,
    FeatureName,
    FeatureStatus,
    TechnicalFeature,
    TechnicalFeatureSet,
)
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateRanking,
    CandidateResult,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.attention import AttentionBand
    from dhruva.shared.identity import InstrumentId

__all__ = ["CandidateInput", "rank_technical_candidates"]

_ESSENTIAL = (
    FeatureName.RETURN_120,
    FeatureName.EXCESS_RETURN_60,
    FeatureName.EXCESS_RETURN_120,
    FeatureName.REALIZED_VOL_60,
    FeatureName.DRAWDOWN_126,
    FeatureName.VOLUME_RATIO_20_MEDIAN,
    FeatureName.MEDIAN_RUPEE_TURNOVER_20,
)
_SCORE_QUANTUM = Decimal("0.01")
_PERCENTILE_QUANTUM = Decimal("0.1")
_GROUP_WEIGHTS: Final = {
    "relative60": Decimal(15),
    "relative120": Decimal(20),
    "trend50": Decimal(10),
    "trend200": Decimal(10),
    "risk_adjusted": Decimal(20),
    "volume": Decimal(5),
    "turnover": Decimal(5),
    "volatility": Decimal(5),
    "drawdown": Decimal(5),
    "regime": Decimal(5),
}


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """One owner instrument plus analytics and separate attention metadata."""

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    features: TechnicalFeatureSet
    attention_score: int | None = None
    attention_band: AttentionBand | None = None
    attention_cutoff: datetime | None = None


def rank_technical_candidates(
    inputs: tuple[CandidateInput, ...], *, cutoff: datetime
) -> CandidateRanking:
    """Rank eligible stocks with cross-sectional midrank percentiles."""
    if any(item.instrument_id != item.features.instrument_id for item in inputs):
        raise ValidationError("candidate input and technical features name different instruments")
    if any(item.features.cutoff != cutoff for item in inputs):
        raise ValidationError("candidate ranking mixes technical feature cutoffs")
    if len({item.instrument_id for item in inputs}) != len(inputs):
        raise ValidationError("candidate ranking contains a duplicate instrument")
    if len({item.canonical_symbol for item in inputs}) != len(inputs):
        raise ValidationError("candidate ranking contains a duplicate symbol")
    ordered = tuple(sorted(inputs, key=lambda item: item.canonical_symbol))
    eligibility = {item.instrument_id: _eligibility(item.features) for item in ordered}
    eligible = tuple(
        item for item in ordered if eligibility[item.instrument_id] is CandidateEligibility.ELIGIBLE
    )
    metrics = _metrics(eligible)
    # Optional factors deliberately omit instruments without a value.  Each
    # percentile distribution therefore has its own PIT-observable membership.
    percentiles = {name: _percentiles(values) for name, values in metrics.items()}

    provisional: list[CandidateResult] = []
    for item in eligible:
        score = _score(item, percentiles)
        provisional.append(
            CandidateResult(
                instrument_id=item.instrument_id,
                canonical_symbol=item.canonical_symbol,
                company_name=item.company_name,
                cutoff=cutoff,
                eligibility=CandidateEligibility.ELIGIBLE,
                score=score,
                rank=1,
                universe_percentile=Decimal(0),
                relative_strength_60_percentile=percentiles["relative60"][
                    item.instrument_id
                ].quantize(_PERCENTILE_QUANTUM, rounding=ROUND_HALF_UP),
                relative_strength_120_percentile=percentiles["relative120"][
                    item.instrument_id
                ].quantize(_PERCENTILE_QUANTUM, rounding=ROUND_HALF_UP),
                tier=_tier(score),
                evidence_completeness=_completeness(item.features),
                supporting_evidence=_supporting(item, percentiles, len(eligible)),
                counterevidence=_counterevidence(item),
                missing_evidence=_missing(item.features),
                feature_availability=tuple(
                    (feature.name.value, feature.status.value) for feature in item.features.features
                ),
                attention_score=item.attention_score,
                attention_band=item.attention_band,
                attention_cutoff=item.attention_cutoff,
            )
        )
    ranked = sorted(provisional, key=_candidate_sort_key)
    score_percentiles = _percentiles(
        {item.instrument_id: _candidate_score(item) for item in ranked}
    )
    completed = [
        replace(
            item,
            rank=index,
            universe_percentile=score_percentiles[item.instrument_id].quantize(
                _PERCENTILE_QUANTUM, rounding=ROUND_HALF_UP
            ),
        )
        for index, item in enumerate(ranked, start=1)
    ]
    excluded = [
        _excluded(item, eligibility[item.instrument_id], cutoff=cutoff)
        for item in ordered
        if eligibility[item.instrument_id] is not CandidateEligibility.ELIGIBLE
    ]
    return CandidateRanking(
        cutoff=cutoff,
        entries=(*completed, *excluded),
        benchmark_symbol="NIFTY 50",
        benchmark_basis=BenchmarkBasis.PRICE_INDEX.value,
        feature_revision=TECHNICAL_FEATURE_REVISION,
        limitations=(
            "NIFTY 50 is a price index, not TRI; dividends are excluded",
            "current owner watchlist is not a survivorship-safe evaluation universe",
            "fundamentals and news do not contribute to technical-candidate-v0",
            "the baseline is experimental and has not passed walk-forward evaluation",
        ),
    )


def _eligibility(features: TechnicalFeatureSet) -> CandidateEligibility:
    statuses = tuple(item.status for item in features.features)
    if FeatureStatus.STALE in statuses:
        return CandidateEligibility.STALE
    if features.adjustment_status is AdjustmentEvidence.RAW:
        return CandidateEligibility.UNSUPPORTED_ADJUSTMENT
    essential = tuple(features.get(name) for name in _ESSENTIAL)
    if any(item.status is FeatureStatus.BENCHMARK_UNAVAILABLE for item in essential):
        return CandidateEligibility.BENCHMARK_UNAVAILABLE
    if any(item.status is FeatureStatus.DATA_UNAVAILABLE for item in essential):
        return CandidateEligibility.DATA_UNAVAILABLE
    if any(item.status is not FeatureStatus.AVAILABLE for item in essential):
        return CandidateEligibility.INSUFFICIENT_HISTORY
    return CandidateEligibility.ELIGIBLE


def _metrics(inputs: tuple[CandidateInput, ...]) -> dict[str, dict[InstrumentId, Decimal]]:
    metrics: dict[str, dict[InstrumentId, Decimal]] = {
        name: {}
        for name in (
            "relative60",
            "relative120",
            "trend50",
            "trend200",
            "risk_adjusted",
            "volume",
            "turnover",
            "volatility",
            "drawdown",
        )
    }
    for item in inputs:
        get = item.features.get
        metrics["relative60"][item.instrument_id] = _value(get(FeatureName.EXCESS_RETURN_60))
        metrics["relative120"][item.instrument_id] = _value(get(FeatureName.EXCESS_RETURN_120))
        metrics["trend50"][item.instrument_id] = _value(get(FeatureName.CLOSE_VS_MA50))
        trend200 = get(FeatureName.MA50_VS_MA200)
        if trend200.value is not None:
            metrics["trend200"][item.instrument_id] = trend200.value
        metrics["risk_adjusted"][item.instrument_id] = _value(get(FeatureName.RETURN_120)) / max(
            _value(get(FeatureName.REALIZED_VOL_60)), Decimal("0.000001")
        )
        metrics["volume"][item.instrument_id] = _value(get(FeatureName.VOLUME_RATIO_20_MEDIAN))
        metrics["turnover"][item.instrument_id] = _value(get(FeatureName.MEDIAN_RUPEE_TURNOVER_20))
        metrics["volatility"][item.instrument_id] = -_value(get(FeatureName.REALIZED_VOL_60))
        metrics["drawdown"][item.instrument_id] = _value(get(FeatureName.DRAWDOWN_126))
    return metrics


def _percentiles(values: dict[InstrumentId, Decimal]) -> dict[InstrumentId, Decimal]:
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): Decimal(50)}
    ordered = sorted(values.items(), key=lambda item: (item[1], str(item[0])))
    result: dict[InstrumentId, Decimal] = {}
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        midrank = (Decimal(index) + Decimal(end)) / Decimal(2)
        percentile = midrank / Decimal(len(ordered) - 1) * Decimal(100)
        for position in range(index, end + 1):
            result[ordered[position][0]] = percentile
        index = end + 1
    return result


def _score(
    item: CandidateInput,
    percentiles: dict[str, dict[InstrumentId, Decimal]],
) -> Decimal:
    components: list[tuple[Decimal, Decimal]] = []
    for name in (
        "relative60",
        "relative120",
        "trend50",
        "risk_adjusted",
        "volume",
        "turnover",
        "volatility",
        "drawdown",
    ):
        components.append((_GROUP_WEIGHTS[name], _center(percentiles[name][item.instrument_id])))
    if item.instrument_id in percentiles["trend200"]:
        components.append(
            (_GROUP_WEIGHTS["trend200"], _center(percentiles["trend200"][item.instrument_id]))
        )
    regime = {
        BenchmarkRegime.RISK_ON: Decimal(1),
        BenchmarkRegime.MIXED: Decimal(0),
        BenchmarkRegime.RISK_OFF: Decimal(-1),
        BenchmarkRegime.UNAVAILABLE: Decimal(0),
    }[item.features.benchmark_regime]
    if item.features.benchmark_regime_status is FeatureStatus.AVAILABLE:
        components.append((_GROUP_WEIGHTS["regime"], regime))
    available_weight = sum((weight for weight, _value_ in components), Decimal(0))
    weighted = sum((weight * value for weight, value in components), Decimal(0))
    return (weighted / available_weight * Decimal(100)).quantize(
        _SCORE_QUANTUM, rounding=ROUND_HALF_UP
    )


def _center(percentile: Decimal) -> Decimal:
    return percentile / Decimal(50) - Decimal(1)


def _candidate_sort_key(item: CandidateResult) -> tuple[Decimal, str]:
    """Sort scored provisional candidates without weakening the optional domain type."""
    if item.score is None:  # pragma: no cover - caller passes eligible provisional rows only
        raise ValueError("eligible provisional candidate has no score")
    return -item.score, item.canonical_symbol


def _tier(score: Decimal) -> CandidateTier:
    if score >= Decimal(50):
        return CandidateTier.STRONG_CANDIDATE
    if score >= Decimal(20):
        return CandidateTier.CANDIDATE
    if score >= Decimal(-20):
        return CandidateTier.NEUTRAL
    return CandidateTier.WEAK


def _candidate_score(item: CandidateResult) -> Decimal:
    if item.score is None:  # pragma: no cover - caller passes eligible provisional rows only
        raise ValueError("eligible candidate has no score")
    return item.score


def _completeness(features: TechnicalFeatureSet) -> EvidenceCompleteness:
    available = sum(item.status is FeatureStatus.AVAILABLE for item in features.features)
    if features.adjustment_status is AdjustmentEvidence.UNKNOWN:
        return EvidenceCompleteness.LOW
    if (
        available == len(features.features)
        and features.benchmark_basis is BenchmarkBasis.TOTAL_RETURN_INDEX
    ):
        return EvidenceCompleteness.HIGH
    return EvidenceCompleteness.MODERATE


def _supporting(
    item: CandidateInput,
    percentiles: dict[str, dict[InstrumentId, Decimal]],
    universe_size: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for sessions, metric in ((60, "relative60"), (120, "relative120")):
        relative = percentiles[metric][item.instrument_id]
        if relative >= Decimal(50):
            position = 1 + sum(value > relative for value in percentiles[metric].values())
            reasons.append(
                f"{sessions}-session benchmark-relative momentum ranks {position}/{universe_size}"
            )
    trend = item.features.get(FeatureName.CLOSE_VS_MA50).value
    if trend is not None and trend > 0:
        reasons.append(f"close is {_percent(trend)} above its 50-session average")
    volatility = percentiles["volatility"][item.instrument_id]
    if volatility >= Decimal(50):
        reasons.append("60-session realized volatility is at or below the universe median")
    return tuple(reasons)


def _counterevidence(item: CandidateInput) -> tuple[str, ...]:
    reasons: list[str] = []
    drawdown = item.features.get(FeatureName.DRAWDOWN_126).value
    volume = item.features.get(FeatureName.VOLUME_RATIO_20_MEDIAN).value
    if drawdown is not None and drawdown <= Decimal("-0.10"):
        reasons.append(f"drawdown from the trailing 126-session close high is {_percent(drawdown)}")
    if volume is not None and volume < Decimal(1):
        reasons.append(
            f"current volume is {volume.quantize(Decimal('0.01'))}x its 20-session median"
        )
    if item.features.benchmark_regime is BenchmarkRegime.RISK_OFF:
        reasons.append("NIFTY 50 price-index regime is RISK_OFF")
    return tuple(reasons)


def _missing(features: TechnicalFeatureSet) -> tuple[str, ...]:
    missing = [
        f"{item.name.value}: {item.status.value} ({item.reason})"
        for item in features.features
        if item.status is not FeatureStatus.AVAILABLE
    ]
    missing.extend(features.limitations)
    missing.append("fundamental evidence is not part of technical-candidate-v0")
    return tuple(dict.fromkeys(missing))


def _excluded(
    item: CandidateInput, eligibility: CandidateEligibility, *, cutoff: datetime
) -> CandidateResult:
    return CandidateResult(
        instrument_id=item.instrument_id,
        canonical_symbol=item.canonical_symbol,
        company_name=item.company_name,
        cutoff=cutoff,
        eligibility=eligibility,
        score=None,
        rank=None,
        universe_percentile=None,
        relative_strength_60_percentile=None,
        relative_strength_120_percentile=None,
        tier=None,
        evidence_completeness=EvidenceCompleteness.LOW,
        supporting_evidence=(),
        counterevidence=(),
        missing_evidence=_missing(item.features),
        feature_availability=tuple(
            (feature.name.value, feature.status.value) for feature in item.features.features
        ),
        attention_score=item.attention_score,
        attention_band=item.attention_band,
        attention_cutoff=item.attention_cutoff,
    )


def _value(feature: TechnicalFeature) -> Decimal:
    value = feature.value
    if value is None:
        raise ValueError("ranker attempted to read an unavailable feature")
    return value


def _percent(value: Decimal) -> str:
    return f"{(value * Decimal(100)).quantize(Decimal('0.01'))}%"
