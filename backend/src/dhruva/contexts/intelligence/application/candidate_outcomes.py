"""Materialize matured future outcomes for frozen candidate observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from dhruva.contexts.analytics.api import (
    CANDIDATE_OUTCOME_REVISION,
    DEFAULT_EVALUATION_HORIZONS,
    AdjustmentEvidence,
    BenchmarkBasis,
    FutureOutcome,
    OutcomeStatus,
    calculate_future_outcome,
)
from dhruva.contexts.intelligence.domain.candidate_observation import StoredCandidateObservation
from dhruva.contexts.intelligence.domain.candidate_outcome import (
    CandidateOutcome,
    candidate_outcome_fingerprint,
)
from dhruva.contexts.intelligence.domain.candidates import CandidateEligibility
from dhruva.contexts.intelligence.domain.model_evidence import (
    TECHNICAL_CANDIDATE_EVALUATION_REVISION,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import date, datetime

    from dhruva.contexts.analytics.api import TechnicalSeries
    from dhruva.contexts.intelligence.domain.ports import CandidateObservationUnitOfWork
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "MaterializeCandidateOutcomes",
    "MaterializeCandidateOutcomesCommand",
    "OutcomeClockStatus",
]

_EXECUTION_TIMING = (
    "signal after completed session T; entry next session open; exit Nth session close"
)


@dataclass(frozen=True, slots=True)
class MaterializeCandidateOutcomesCommand:
    """Explicit local facts and assumptions for one idempotent materialization."""

    account_id: AccountId
    stocks: Mapping[InstrumentId, TechnicalSeries]
    stock_bar_revisions: Mapping[InstrumentId, Mapping[date, str]]
    benchmark: TechnicalSeries | None
    benchmark_bar_revisions: Mapping[date, str]
    observable_through: date
    materialized_at: datetime
    horizons: tuple[int, ...] = DEFAULT_EVALUATION_HORIZONS
    cost_bps: Decimal = Decimal("20")
    observation_limit: int = 10_000


@dataclass(frozen=True, slots=True)
class OutcomeClockStatus:
    """Counts on the prospective evidence clock after one local pass."""

    observations: int
    eligible_members: int
    awaiting_outcomes: int
    mature_outcomes: int
    outcomes_created: int
    outcomes_already_present: int
    degraded_outcomes: int
    unavailable_outcomes: int


class MaterializeCandidateOutcomes:
    """Join frozen candidates to later bars and append only mature outcomes."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], CandidateObservationUnitOfWork],
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, command: MaterializeCandidateOutcomesCommand) -> OutcomeClockStatus:
        """Materialize mature facts; pending/missing states remain computed status."""
        awaiting = 0
        mature = 0
        created = 0
        existing = 0
        unavailable = 0
        degraded = 0
        eligible_members = 0
        inspected = 0
        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            observations = await unit_of_work.candidate_observations.list_recent(
                limit=command.observation_limit
            )
            for stored in observations:
                if stored.observation.recorded_at > command.materialized_at:
                    continue
                inspected += 1
                ranking = stored.observation.ranking
                for candidate in ranking.entries:
                    if candidate.eligibility is not CandidateEligibility.ELIGIBLE:
                        continue
                    eligible_members += 1
                    stock = command.stocks.get(candidate.instrument_id)
                    for horizon in command.horizons:
                        if _benchmark_horizon_is_pending(
                            command.benchmark,
                            signal_cutoff=ranking.cutoff,
                            observable_through=command.observable_through,
                            horizon_sessions=horizon,
                        ):
                            awaiting += 1
                            continue
                        if stock is None:
                            unavailable += 1
                            continue
                        outcome = calculate_future_outcome(
                            stock=stock,
                            benchmark=command.benchmark,
                            signal_cutoff=ranking.cutoff,
                            observable_through=command.observable_through,
                            horizon_sessions=horizon,
                            benchmark_basis=BenchmarkBasis(ranking.benchmark_basis),
                            cost_bps=command.cost_bps,
                            benchmark_symbol=ranking.benchmark_symbol,
                        )
                        if outcome.status is OutcomeStatus.PENDING:
                            awaiting += 1
                            continue
                        if outcome.status in {
                            OutcomeStatus.CORPORATE_ACTION_UNVERIFIED,
                            OutcomeStatus.UNSUPPORTED_ADJUSTMENT,
                        }:
                            degraded += 1
                            continue
                        if outcome.status is not OutcomeStatus.MATURE:
                            unavailable += 1
                            continue
                        mature += 1
                        fact = _outcome_fact(
                            command=command,
                            stored=stored,
                            candidate_symbol=candidate.canonical_symbol,
                            outcome=outcome,
                        )
                        result = await unit_of_work.candidate_outcomes.append(fact)
                        if result.created:
                            created += 1
                        else:
                            existing += 1
            await unit_of_work.commit()
        return OutcomeClockStatus(
            observations=inspected,
            eligible_members=eligible_members,
            awaiting_outcomes=awaiting,
            mature_outcomes=mature,
            outcomes_created=created,
            outcomes_already_present=existing,
            degraded_outcomes=degraded,
            unavailable_outcomes=unavailable,
        )


def _benchmark_horizon_is_pending(
    benchmark: TechnicalSeries | None,
    *,
    signal_cutoff: datetime,
    observable_through: date,
    horizon_sessions: int,
) -> bool:
    """Give temporal immaturity precedence over member-data availability.

    A newly frozen ranking cannot have a missing future stock path yet: the path
    does not exist.  Once the benchmark calendar shows that the horizon has
    matured, missing member observations become an actual availability failure.
    """
    if benchmark is None:
        return False
    observable = sum(
        signal_cutoff.date() < bar.trading_date <= observable_through for bar in benchmark.bars
    )
    return observable < horizon_sessions


def _outcome_fact(
    *,
    command: MaterializeCandidateOutcomesCommand,
    stored: StoredCandidateObservation,
    candidate_symbol: str,
    outcome: FutureOutcome,
) -> CandidateOutcome:
    """Build a self-hashing outcome from a mature analytics result."""
    ranking = stored.observation.ranking
    required = (
        outcome.entry_date,
        outcome.entry_price,
        outcome.exit_date,
        outcome.exit_price,
        outcome.absolute_return,
        outcome.benchmark_return,
        outcome.excess_return,
        outcome.net_return,
        outcome.net_excess_return,
        outcome.maximum_adverse_excursion,
        outcome.maximum_favorable_excursion,
        outcome.holding_period_drawdown,
        outcome.realized_volatility,
    )
    if any(value is None for value in required):  # pragma: no cover - mature invariant proves it
        raise ValueError("mature outcome is incomplete")
    entry_date = cast("date", outcome.entry_date)
    exit_date = cast("date", outcome.exit_date)
    limitations = tuple(
        item
        for item in (
            outcome.limitation,
            "NIFTY 50 benchmark is PRICE_INDEX; dividends are excluded",
            "current owner watchlist is not survivorship-safe historical evidence",
        )
        if item
    )
    draft = CandidateOutcome(
        account_id=command.account_id,
        candidate_observation_id=stored.id,
        candidate_observation_sha256=stored.observation.observation_sha256,
        instrument_id=outcome.instrument_id,
        canonical_symbol=candidate_symbol,
        signal_cutoff=outcome.signal_cutoff,
        observable_through=exit_date,
        materialized_at=command.materialized_at,
        horizon_sessions=outcome.horizon_sessions,
        entry_date=entry_date,
        entry_price=cast("Decimal", outcome.entry_price),
        exit_date=exit_date,
        exit_price=cast("Decimal", outcome.exit_price),
        absolute_return=cast("Decimal", outcome.absolute_return),
        benchmark_return=cast("Decimal", outcome.benchmark_return),
        excess_return=cast("Decimal", outcome.excess_return),
        net_return=cast("Decimal", outcome.net_return),
        net_excess_return=cast("Decimal", outcome.net_excess_return),
        maximum_adverse_excursion=cast("Decimal", outcome.maximum_adverse_excursion),
        maximum_favorable_excursion=cast("Decimal", outcome.maximum_favorable_excursion),
        holding_period_drawdown=cast("Decimal", outcome.holding_period_drawdown),
        realized_volatility=cast("Decimal", outcome.realized_volatility),
        ranker_revision=ranking.ranker_revision,
        feature_revision=ranking.feature_revision,
        evaluation_revision=TECHNICAL_CANDIDATE_EVALUATION_REVISION,
        outcome_revision=CANDIDATE_OUTCOME_REVISION,
        benchmark_symbol=ranking.benchmark_symbol,
        benchmark_basis=ranking.benchmark_basis,
        execution_timing=_EXECUTION_TIMING,
        cost_bps=command.cost_bps,
        adjustment_status=cast("AdjustmentEvidence", outcome.adjustment_status).value,
        limitations=limitations,
        stock_bar_revisions=tuple(
            revision
            for day, revision in sorted(
                command.stock_bar_revisions.get(outcome.instrument_id, {}).items()
            )
            if entry_date <= day <= exit_date
        ),
        benchmark_bar_revisions=tuple(
            revision
            for day, revision in sorted(command.benchmark_bar_revisions.items())
            if entry_date <= day <= exit_date
        ),
        outcome_sha256="",
    )
    return replace(draft, outcome_sha256=candidate_outcome_fingerprint(draft))
