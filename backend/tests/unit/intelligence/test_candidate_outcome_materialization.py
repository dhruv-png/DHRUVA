"""Prospective outcome materialization has stable window-bounded provenance."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    BenchmarkBasis,
    TechnicalBar,
    TechnicalSeries,
    calculate_future_outcome,
)
from dhruva.contexts.intelligence.application.candidate_outcomes import (
    MaterializeCandidateOutcomes,
    MaterializeCandidateOutcomesCommand,
    _outcome_fact,
)
from dhruva.contexts.intelligence.domain.candidate_observation import (
    CandidateObservation,
    StoredCandidateObservation,
    candidate_observation_fingerprint,
    candidate_universe_fingerprint,
)
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateRanking,
    CandidateResult,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.contexts.intelligence.domain.ports import CandidateObservationUnitOfWork
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
STOCK = InstrumentId.deterministic("reference", "hal")
BENCHMARK = InstrumentId.deterministic("reference", "nifty")
SIGNAL = datetime(2026, 1, 2, 10, tzinfo=UTC)


def _series(identity: InstrumentId) -> TechnicalSeries:
    return TechnicalSeries(
        instrument_id=identity,
        adjustment_status=AdjustmentEvidence.UNKNOWN,
        bars=tuple(
            TechnicalBar(
                trading_date=SIGNAL.date() + timedelta(days=index + 1),
                open=Decimal(100 + index),
                high=Decimal(102 + index),
                low=Decimal(99 + index),
                close=Decimal(101 + index),
                volume=1_000,
            )
            for index in range(25)
        ),
    )


def _stored() -> StoredCandidateObservation:
    ranking = CandidateRanking(
        cutoff=SIGNAL,
        entries=(
            CandidateResult(
                instrument_id=STOCK,
                canonical_symbol="HAL",
                company_name="Hindustan Aeronautics Limited",
                cutoff=SIGNAL,
                eligibility=CandidateEligibility.ELIGIBLE,
                score=Decimal(25),
                rank=1,
                universe_percentile=Decimal(50),
                relative_strength_60_percentile=Decimal(50),
                relative_strength_120_percentile=Decimal(50),
                tier=CandidateTier.CANDIDATE,
                evidence_completeness=EvidenceCompleteness.LOW,
                supporting_evidence=(),
                counterevidence=(),
                missing_evidence=("adjustment UNKNOWN",),
                feature_availability=(("RETURN_120", "AVAILABLE"),),
            ),
        ),
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        feature_revision="technical-features-v0",
        limitations=("diagnostic",),
    )
    universe_sha = candidate_universe_fingerprint(ranking)
    observation = CandidateObservation(
        account_id=ACCOUNT,
        ranking=ranking,
        recorded_at=SIGNAL,
        universe_sha256=universe_sha,
        observation_sha256=candidate_observation_fingerprint(
            account_id=ACCOUNT,
            ranking=ranking,
            universe_sha256=universe_sha,
        ),
    )
    return StoredCandidateObservation(
        id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        observation=observation,
    )


def test_later_bars_do_not_change_an_already_mature_outcome_identity() -> None:
    """Provenance is bounded to entry/exit, so a later rerun is idempotent."""
    stock = _series(STOCK)
    benchmark = _series(BENCHMARK)
    outcome = calculate_future_outcome(
        stock=stock,
        benchmark=benchmark,
        signal_cutoff=SIGNAL,
        observable_through=date(2026, 1, 27),
        horizon_sessions=20,
        benchmark_basis=BenchmarkBasis.PRICE_INDEX,
        cost_bps=Decimal(20),
    )
    stock_revisions = {bar.trading_date: "1" * 64 for bar in stock.bars}
    benchmark_revisions = {bar.trading_date: "2" * 64 for bar in benchmark.bars}
    command = MaterializeCandidateOutcomesCommand(
        account_id=ACCOUNT,
        stocks={STOCK: stock},
        stock_bar_revisions={STOCK: stock_revisions},
        benchmark=benchmark,
        benchmark_bar_revisions=benchmark_revisions,
        observable_through=date(2026, 1, 27),
        materialized_at=datetime(2026, 1, 27, 12, tzinfo=UTC),
    )
    stored = _stored()
    first = _outcome_fact(
        command=command,
        stored=stored,
        candidate_symbol="HAL",
        outcome=outcome,
    )
    later_day = date(2026, 2, 1)
    later = _outcome_fact(
        command=replace(
            command,
            stock_bar_revisions={STOCK: {**stock_revisions, later_day: "3" * 64}},
            benchmark_bar_revisions={**benchmark_revisions, later_day: "4" * 64},
        ),
        stored=stored,
        candidate_symbol="HAL",
        outcome=outcome,
    )

    assert first == later
    assert first.outcome_sha256 == later.outcome_sha256
    assert len(first.stock_bar_revisions) == 20
    assert len(first.benchmark_bar_revisions) == 20


@pytest.mark.asyncio
async def test_temporal_immaturity_precedes_missing_member_data() -> None:
    """A fresh weekend freeze is pending even when no future stock series can load yet."""
    stored = _stored()

    class Observations:
        async def list_recent(self, *, limit: int) -> tuple[StoredCandidateObservation, ...]:
            assert limit == 10_000
            return (stored,)

    class Outcomes:
        async def append(self, _outcome: object) -> None:
            raise AssertionError("pending evidence must not append an outcome")

    class UnitOfWork:
        candidate_observations = Observations()
        candidate_outcomes = Outcomes()

        async def __aenter__(self) -> UnitOfWork:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    benchmark = TechnicalSeries(
        instrument_id=BENCHMARK,
        adjustment_status=AdjustmentEvidence.UNKNOWN,
        bars=(
            TechnicalBar(
                trading_date=SIGNAL.date(),
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=1_000,
            ),
        ),
    )
    result = await MaterializeCandidateOutcomes(
        lambda _account: cast("CandidateObservationUnitOfWork", UnitOfWork())
    ).execute(
        MaterializeCandidateOutcomesCommand(
            account_id=ACCOUNT,
            stocks={},
            stock_bar_revisions={},
            benchmark=benchmark,
            benchmark_bar_revisions={},
            observable_through=SIGNAL.date(),
            materialized_at=SIGNAL + timedelta(hours=1),
        )
    )

    assert result.awaiting_outcomes == 2
    assert result.degraded_outcomes == 0
    assert result.unavailable_outcomes == 0
