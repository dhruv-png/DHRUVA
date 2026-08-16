"""Historical replay and evidence metrics are PIT-safe and deterministic."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    BenchmarkBasis,
    FeatureName,
    FeatureStatus,
    TechnicalBar,
    TechnicalSeries,
    compute_technical_features,
)
from dhruva.contexts.intelligence.application.candidate_evaluation import (
    build_evaluation_dataset,
    select_weekly_cutoffs,
)
from dhruva.contexts.intelligence.application.candidate_ranking import (
    CandidateInput,
    rank_technical_candidates,
)
from dhruva.contexts.intelligence.domain.candidates import CandidateEligibility, CandidateRanking
from dhruva.contexts.intelligence.domain.model_evidence import (
    TECHNICAL_CANDIDATE_EVALUATION_REVISION,
    EvaluationIdentity,
    UniverseType,
    build_evaluation_metrics,
    build_evaluation_readiness,
    evidence_export_bytes,
)
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

CUTOFF = datetime(2025, 10, 1, 10, tzinfo=UTC)
BENCHMARK_ID = InstrumentId.deterministic("reference", "nifty")


def _series(
    identity: InstrumentId, daily: Decimal, *, future_jump: bool = False
) -> TechnicalSeries:
    prices = [Decimal(100)]
    for _index in range(279):
        prices.append(prices[-1] * (Decimal(1) + daily))
    start = CUTOFF.date() - timedelta(days=220)
    bars = [
        TechnicalBar(
            trading_date=start + timedelta(days=index),
            open=price,
            high=price * Decimal("1.01"),
            low=price * Decimal("0.99"),
            close=price,
            volume=100_000,
        )
        for index, price in enumerate(prices)
    ]
    if future_jump:
        bars[-1] = replace(bars[-1], close=bars[-1].close * Decimal(100))
    return TechnicalSeries(
        instrument_id=identity,
        bars=tuple(bars),
        adjustment_status=AdjustmentEvidence.UNKNOWN,
    )


def _ranking(*, future_jump: bool = False) -> tuple[CandidateRanking, TechnicalSeries]:
    benchmark = _series(BENCHMARK_ID, Decimal("0.0004"), future_jump=future_jump)
    inputs = []
    for symbol, daily in (("FAST", Decimal("0.002")), ("SLOW", Decimal("0.0007"))):
        identity = InstrumentId.deterministic("reference", symbol.lower())
        stock = _series(identity, daily, future_jump=future_jump)
        features = compute_technical_features(
            stock=stock,
            benchmark=benchmark,
            cutoff=CUTOFF,
            benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        )
        inputs.append(
            CandidateInput(
                instrument_id=identity,
                canonical_symbol=symbol,
                company_name=f"{symbol} Limited",
                features=features,
            )
        )
    return rank_technical_candidates(tuple(inputs), cutoff=CUTOFF), benchmark


def _identity() -> EvaluationIdentity:
    return EvaluationIdentity(
        ranker_revision="technical-candidate-v0",
        feature_revision="technical-features-v0",
        evaluation_revision=TECHNICAL_CANDIDATE_EVALUATION_REVISION,
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        universe_label="CURRENT OWNER WATCHLIST (RETROSPECTIVE)",
        universe_type=UniverseType.RETROSPECTIVE_CURRENT_WATCHLIST,
        from_cutoff=CUTOFF.date(),
        to_cutoff=CUTOFF.date(),
        cadence="WEEKLY_FINAL_OBSERVED_TRADING_SESSION",
        horizons=(20,),
        execution_timing="after T, next session",
        entry_price_basis="NEXT_SESSION_OPEN",
        exit_price_basis="NTH_HOLDING_SESSION_CLOSE",
        cost_bps=Decimal(20),
        knowledge_cutoff=CUTOFF + timedelta(days=80),
        limitations=("retrospective diagnostic",),
    )


def test_future_bars_cannot_change_historical_features_scores_or_ranks() -> None:
    """Stock and benchmark bars after T are excluded before feature calculation."""
    normal, _benchmark = _ranking()
    jumped, _jumped_benchmark = _ranking(future_jump=True)

    assert normal == jumped


def test_historical_replay_ranks_partial_optional_history_without_leakage() -> None:
    """The replay ranker keeps an eligible stock whose MA200 factor is not yet observable."""

    def observed_series(
        identity: InstrumentId,
        daily: Decimal,
        *,
        sessions: int,
        future_jump: bool = False,
    ) -> TechnicalSeries:
        prices = [Decimal(100)]
        for _index in range(sessions - 1):
            prices.append(prices[-1] * (Decimal(1) + daily))
        start = CUTOFF.date() - timedelta(days=sessions - 1)
        bars = [
            TechnicalBar(
                trading_date=start + timedelta(days=index),
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=100_000,
            )
            for index, price in enumerate(prices)
        ]
        if future_jump:
            price = bars[-1].close * Decimal(100)
            bars.append(
                TechnicalBar(
                    trading_date=CUTOFF.date() + timedelta(days=1),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=100_000,
                )
            )
        return TechnicalSeries(
            instrument_id=identity,
            bars=tuple(bars),
            adjustment_status=AdjustmentEvidence.UNKNOWN,
        )

    benchmark = observed_series(BENCHMARK_ID, Decimal("0.0004"), sessions=220)
    identities = {
        "PARTIAL": InstrumentId.deterministic("reference", "partial"),
        "COMPLETE": InstrumentId.deterministic("reference", "complete"),
    }

    def replay(*, future_jump: bool) -> CandidateRanking:
        inputs = []
        for symbol, sessions, daily in (
            ("PARTIAL", 150, Decimal("0.0015")),
            ("COMPLETE", 220, Decimal("0.0008")),
        ):
            stock = observed_series(
                identities[symbol], daily, sessions=sessions, future_jump=future_jump
            )
            inputs.append(
                CandidateInput(
                    instrument_id=identities[symbol],
                    canonical_symbol=symbol,
                    company_name=f"{symbol} Limited",
                    features=compute_technical_features(
                        stock=stock,
                        benchmark=benchmark,
                        cutoff=CUTOFF,
                        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
                    ),
                )
            )
        return rank_technical_candidates(tuple(inputs), cutoff=CUTOFF)

    ranking = replay(future_jump=False)
    partial = next(item for item in ranking.entries if item.canonical_symbol == "PARTIAL")

    assert partial.eligibility is CandidateEligibility.ELIGIBLE
    assert partial.score is not None
    assert partial.relative_strength_120_percentile is not None
    assert (
        FeatureName.MA50_VS_MA200.value,
        FeatureStatus.INSUFFICIENT_HISTORY.value,
    ) in partial.feature_availability
    assert any("MA50_VS_MA200: INSUFFICIENT_HISTORY" in item for item in partial.missing_evidence)
    assert ranking == replay(future_jump=True)


def test_weekly_cutoff_is_last_observed_session_not_calendar_friday() -> None:
    """A Thursday is chosen when the persisted market calendar has no Friday."""
    sessions = (
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 12),
    )

    assert select_weekly_cutoffs(
        sessions, from_date=date(2026, 1, 1), to_date=date(2026, 1, 12)
    ) == (date(2026, 1, 8), date(2026, 1, 12))


def test_dataset_metrics_readiness_and_export_are_deterministic() -> None:
    """Identical persisted inputs produce identical rows, metrics, and bytes."""
    ranking, benchmark = _ranking()
    stocks = {
        entry.instrument_id: _series(
            entry.instrument_id,
            Decimal("0.002") if entry.canonical_symbol == "FAST" else Decimal("0.0007"),
        )
        for entry in ranking.entries
    }
    dataset = build_evaluation_dataset(
        identity=_identity(),
        rankings=(ranking,),
        stocks=stocks,
        benchmark=benchmark,
        observable_through=CUTOFF.date() + timedelta(days=60),
    )
    metrics = build_evaluation_metrics(dataset)
    readiness = build_evaluation_readiness(
        dataset,
        sessions_available=496,
        adjustment_semantics="UNKNOWN",
    )

    assert readiness.status.value == "DIAGNOSTIC_ONLY"
    assert metrics.cross_sectional[0].observation_count == 2
    assert metrics.top_k[0].k == 1
    assert evidence_export_bytes(dataset, metrics, readiness) == evidence_export_bytes(
        dataset, metrics, readiness
    )
    assert b"dhruva.model-evidence.v1" in evidence_export_bytes(dataset, metrics, readiness)
