"""The experimental candidate baseline is deterministic and separate from attention."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    BenchmarkBasis,
    FeatureName,
    FeatureStatus,
    TechnicalBar,
    TechnicalFeatureSet,
    TechnicalSeries,
    compute_technical_features,
)
from dhruva.contexts.intelligence.application import candidate_ranking as ranking_module
from dhruva.contexts.intelligence.application.candidate_ranking import (
    CandidateInput,
    rank_technical_candidates,
)
from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

CUTOFF = datetime(2026, 8, 10, 12, tzinfo=UTC)
BENCHMARK = InstrumentId.deterministic("reference", "nse-index-nifty-50")


def _series(
    identity: InstrumentId,
    *,
    daily_return: Decimal,
    count: int = 220,
    volume: int = 100_000,
    adjustment: AdjustmentEvidence = AdjustmentEvidence.UNKNOWN,
) -> TechnicalSeries:
    prices = [Decimal(100)]
    for _index in range(count - 1):
        prices.append(prices[-1] * (Decimal(1) + daily_return))
    start = CUTOFF.date() - timedelta(days=count)
    return TechnicalSeries(
        instrument_id=identity,
        adjustment_status=adjustment,
        bars=tuple(
            TechnicalBar(
                trading_date=start + timedelta(days=index),
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=volume if index < count - 1 else volume * 2,
            )
            for index, price in enumerate(prices)
        ),
    )


def _features(
    identity: InstrumentId,
    *,
    daily_return: Decimal,
    count: int = 220,
    adjustment: AdjustmentEvidence = AdjustmentEvidence.UNKNOWN,
) -> TechnicalFeatureSet:
    benchmark = _series(BENCHMARK, daily_return=Decimal("0.0005"), count=count)
    return compute_technical_features(
        stock=_series(
            identity,
            daily_return=daily_return,
            count=count,
            adjustment=adjustment,
        ),
        benchmark=benchmark,
        cutoff=CUTOFF,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
    )


def _input(
    symbol: str,
    daily_return: Decimal,
    *,
    count: int = 220,
    attention_score: int | None = None,
) -> CandidateInput:
    identity = InstrumentId.deterministic("reference", symbol.lower())
    return CandidateInput(
        instrument_id=identity,
        canonical_symbol=symbol,
        company_name=f"{symbol} Limited",
        features=_features(identity, daily_return=daily_return, count=count),
        attention_score=attention_score,
        attention_band=AttentionBand.HIGH if attention_score is not None else None,
        attention_cutoff=CUTOFF if attention_score is not None else None,
    )


def test_insufficient_history_is_excluded_without_zero_filling() -> None:
    """Short histories remain visible but never receive an artificial score."""
    ranking = rank_technical_candidates(
        (_input("SHORT", Decimal("0.001"), count=15),), cutoff=CUTOFF
    )
    result = ranking.entries[0]

    assert result.eligibility is CandidateEligibility.INSUFFICIENT_HISTORY
    assert result.score is None
    assert result.rank is None
    assert result.tier is None
    assert result.evidence_completeness is EvidenceCompleteness.LOW
    assert any("RETURN_120: INSUFFICIENT_HISTORY" in item for item in result.missing_evidence)


def test_cross_sectional_order_percentiles_and_ties_are_deterministic() -> None:
    """Cross-sectional scores, percentiles, and stable tie breaks are repeatable."""
    inputs = (
        _input("SLOW", Decimal("0.0007")),
        _input("FASTB", Decimal("0.002")),
        _input("FASTA", Decimal("0.002")),
    )

    first = rank_technical_candidates(inputs, cutoff=CUTOFF)
    second = rank_technical_candidates(tuple(reversed(inputs)), cutoff=CUTOFF)

    assert first == second
    assert [item.canonical_symbol for item in first.entries] == ["FASTA", "FASTB", "SLOW"]
    assert [item.rank for item in first.entries] == [1, 2, 3]
    assert [item.universe_percentile for item in first.entries] == [
        Decimal("75.0"),
        Decimal("75.0"),
        Decimal("0.0"),
    ]
    assert [item.relative_strength_60_percentile for item in first.entries] == [
        Decimal("75.0"),
        Decimal("75.0"),
        Decimal("0.0"),
    ]
    assert [item.relative_strength_120_percentile for item in first.entries] == [
        Decimal("75.0"),
        Decimal("75.0"),
        Decimal("0.0"),
    ]
    assert [item.score for item in first.entries] == [
        Decimal("45.00"),
        Decimal("45.00"),
        Decimal("-75.00"),
    ]
    assert first.entries[0].tier in {CandidateTier.CANDIDATE, CandidateTier.STRONG_CANDIDATE}


def test_optional_factor_percentiles_are_factor_specific_and_renormalized() -> None:
    """An eligible stock without MA200 stays missing rather than crashing or becoming zero."""
    inputs: list[CandidateInput] = []
    settings = (
        ("LOW", Decimal("0.1"), Decimal("0.3"), Decimal("0.01")),
        ("HIGH", Decimal("0.3"), Decimal("0.1"), None),
        ("MID", Decimal("0.2"), Decimal("0.2"), Decimal("0.02")),
    )
    for symbol, level, volatility, trend200 in settings:
        base = _input(symbol, Decimal("0.001"))
        values = {
            FeatureName.RETURN_120: level,
            FeatureName.EXCESS_RETURN_60: level,
            FeatureName.EXCESS_RETURN_120: level,
            FeatureName.CLOSE_VS_MA50: level,
            FeatureName.REALIZED_VOL_60: volatility,
            FeatureName.VOLUME_RATIO_20_MEDIAN: level,
            FeatureName.MEDIAN_RUPEE_TURNOVER_20: level,
            FeatureName.DRAWDOWN_126: -volatility,
        }
        features = tuple(
            replace(feature, value=values[feature.name], status=FeatureStatus.AVAILABLE, reason="")
            if feature.name in values
            else (
                replace(
                    feature,
                    value=trend200,
                    status=(
                        FeatureStatus.AVAILABLE
                        if trend200 is not None
                        else FeatureStatus.INSUFFICIENT_HISTORY
                    ),
                    reason="" if trend200 is not None else "200 sessions required; 150 available",
                )
                if feature.name is FeatureName.MA50_VS_MA200
                else feature
            )
            for feature in base.features.features
        )
        inputs.append(replace(base, features=replace(base.features, features=features)))

    metrics = ranking_module._metrics(tuple(inputs))
    high_id = next(item.instrument_id for item in inputs if item.canonical_symbol == "HIGH")
    assert high_id not in metrics["trend200"]
    assert all(high_id in values for name, values in metrics.items() if name != "trend200")

    first = rank_technical_candidates(tuple(inputs), cutoff=CUTOFF)
    second = rank_technical_candidates(tuple(reversed(inputs)), cutoff=CUTOFF)
    high = next(item for item in first.entries if item.canonical_symbol == "HIGH")

    assert first == second
    assert high.eligibility is CandidateEligibility.ELIGIBLE
    assert high.score == Decimal("100.00")
    assert high.relative_strength_60_percentile == Decimal("100.0")
    assert high.relative_strength_120_percentile == Decimal("100.0")
    assert any("MA50_VS_MA200: INSUFFICIENT_HISTORY" in item for item in high.missing_evidence)

    zero_filled = tuple(
        replace(
            item,
            features=replace(
                item.features,
                features=tuple(
                    replace(feature, status=FeatureStatus.AVAILABLE, value=Decimal(0), reason="")
                    if item.instrument_id == high_id and feature.name is FeatureName.MA50_VS_MA200
                    else feature
                    for feature in item.features.features
                ),
            ),
        )
        for item in inputs
    )
    zero_filled_high = next(
        item
        for item in rank_technical_candidates(zero_filled, cutoff=CUTOFF).entries
        if item.canonical_symbol == "HIGH"
    )
    assert zero_filled_high.score == Decimal("80.00")
    assert zero_filled_high.score != high.score


def test_attention_metadata_never_changes_candidate_score_or_rank() -> None:
    """Attention remains optional metadata outside the candidate ranker."""
    base = _input("ONE", Decimal("0.0015"))
    quiet = replace(
        base,
        attention_score=0,
        attention_band=AttentionBand.LOW,
        attention_cutoff=CUTOFF,
    )
    noisy = replace(
        base,
        attention_score=14,
        attention_band=AttentionBand.HIGH,
        attention_cutoff=CUTOFF,
    )

    quiet_result = rank_technical_candidates((quiet,), cutoff=CUTOFF).entries[0]
    noisy_result = rank_technical_candidates((noisy,), cutoff=CUTOFF).entries[0]

    assert quiet_result.score == noisy_result.score
    assert quiet_result.rank == noisy_result.rank
    assert quiet_result.attention_score == 0
    assert noisy_result.attention_score == 14


def test_explanations_are_exact_computed_evidence_not_generated_prose() -> None:
    """Explanations expose deterministic feature facts and limitations."""
    ranking = rank_technical_candidates(
        (
            _input("FAST", Decimal("0.002")),
            _input("SLOW", Decimal("0.0006")),
        ),
        cutoff=CUTOFF,
    )
    fast = ranking.entries[0]

    assert "60-session benchmark-relative momentum ranks 1/2" in fast.supporting_evidence
    assert "120-session benchmark-relative momentum ranks 1/2" in fast.supporting_evidence
    assert any("above its 50-session average" in item for item in fast.supporting_evidence)
    assert "corporate-action adjustment status is UNKNOWN" in fast.missing_evidence
    assert fast.evidence_completeness is EvidenceCompleteness.LOW


def test_stale_required_features_block_ranking() -> None:
    """Stale essential evidence excludes an instrument from normal ranking."""
    identity = InstrumentId.deterministic("reference", "stale")
    features = _features(identity, daily_return=Decimal("0.001"))
    stale = replace(
        features,
        features=tuple(
            replace(item, status=FeatureStatus.STALE, value=None, reason="stale")
            for item in features.features
        ),
    )
    candidate = CandidateInput(
        instrument_id=identity,
        canonical_symbol="STALE",
        company_name="Stale Limited",
        features=stale,
    )

    result = rank_technical_candidates((candidate,), cutoff=CUTOFF).entries[0]

    assert result.eligibility is CandidateEligibility.STALE
    assert result.score is None


def test_raw_adjustment_is_explicitly_unsupported_but_unknown_is_visible_degradation() -> None:
    """Raw history blocks ranking while unknown adjustment visibly degrades it."""
    identity = InstrumentId.deterministic("reference", "raw")
    raw = _features(
        identity,
        daily_return=Decimal("0.001"),
        adjustment=AdjustmentEvidence.RAW,
    )
    candidate = CandidateInput(
        instrument_id=identity,
        canonical_symbol="RAW",
        company_name="Raw Limited",
        features=raw,
    )

    result = rank_technical_candidates((candidate,), cutoff=CUTOFF).entries[0]

    assert result.eligibility is CandidateEligibility.UNSUPPORTED_ADJUSTMENT
    assert result.score is None
    assert set(result.feature_availability) == {
        (name.value, FeatureStatus.UNSUPPORTED_ADJUSTMENT.value) for name in FeatureName
    }


def test_feature_vocabulary_contains_no_future_outcome() -> None:
    """The baseline feature vocabulary contains no realized future outcome."""
    names = {name.value for name in FeatureName}

    assert not any(token in name for name in names for token in ("FUTURE", "OUTCOME", "LABEL"))
