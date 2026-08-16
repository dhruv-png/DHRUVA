"""Join frozen/replayed candidate rankings to later observable outcomes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from dhruva.contexts.analytics.api import (
    BenchmarkBasis,
    OutcomeStatus,
    calculate_future_outcome,
)
from dhruva.contexts.intelligence.domain.candidates import CandidateResult
from dhruva.contexts.intelligence.domain.model_evidence import (
    EvaluationDataset,
    EvaluationIdentity,
    EvaluationRow,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from dhruva.contexts.analytics.api import TechnicalSeries
    from dhruva.contexts.intelligence.domain.candidates import CandidateRanking
    from dhruva.shared.identity import InstrumentId

__all__ = ["build_evaluation_dataset", "select_weekly_cutoffs"]


def select_weekly_cutoffs(
    sessions: Sequence[date], *, from_date: date, to_date: date
) -> tuple[date, ...]:
    """Choose the final observed trading session in each ISO week.

    The supplied dates are the persisted benchmark session calendar.  This
    avoids assuming that a calendar Friday was open on the exchange.
    """
    selected: dict[tuple[int, int], date] = {}
    for session in sorted(set(sessions)):
        if not from_date <= session <= to_date:
            continue
        iso = session.isocalendar()
        selected[(iso.year, iso.week)] = session
    return tuple(selected[key] for key in sorted(selected))


def build_evaluation_dataset(
    *,
    identity: EvaluationIdentity,
    rankings: tuple[CandidateRanking, ...],
    stocks: Mapping[InstrumentId, TechnicalSeries],
    benchmark: TechnicalSeries | None,
    observable_through: date,
) -> EvaluationDataset:
    """Build stable audit rows without mutating any supplied ranking."""
    rows: list[EvaluationRow] = []
    for ranking in sorted(rankings, key=lambda item: item.cutoff):
        for horizon in identity.horizons:
            for candidate in ranking.entries:
                stock = stocks.get(candidate.instrument_id)
                if stock is None:
                    rows.append(
                        _row(
                            candidate,
                            horizon=horizon,
                            outcome_status=OutcomeStatus.DATA_UNAVAILABLE.value,
                            limitation="stock daily history is unavailable",
                        )
                    )
                    continue
                outcome = calculate_future_outcome(
                    stock=stock,
                    benchmark=benchmark,
                    signal_cutoff=ranking.cutoff,
                    observable_through=observable_through,
                    horizon_sessions=horizon,
                    benchmark_basis=BenchmarkBasis(identity.benchmark_basis),
                    cost_bps=identity.cost_bps,
                    benchmark_symbol=identity.benchmark_symbol,
                )
                rows.append(
                    _row(
                        candidate,
                        horizon=horizon,
                        outcome_status=outcome.status.value,
                        absolute_return=outcome.absolute_return,
                        benchmark_return=outcome.benchmark_return,
                        excess_return=outcome.excess_return,
                        net_return=outcome.net_return,
                        net_excess_return=outcome.net_excess_return,
                        maximum_adverse_excursion=outcome.maximum_adverse_excursion,
                        maximum_favorable_excursion=outcome.maximum_favorable_excursion,
                        holding_period_drawdown=outcome.holding_period_drawdown,
                        realized_volatility=outcome.realized_volatility,
                        entry_date=outcome.entry_date,
                        exit_date=outcome.exit_date,
                        limitation=outcome.limitation,
                    )
                )
    ordered = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.cutoff,
                row.horizon_sessions,
                row.rank is None,
                row.rank or 0,
                row.canonical_symbol,
            ),
        )
    )
    return EvaluationDataset(identity=identity, rows=ordered)


def _row(  # noqa: PLR0913 - explicit evidence columns are the audit contract
    candidate: CandidateResult,
    *,
    horizon: int,
    outcome_status: str,
    absolute_return: Decimal | None = None,
    benchmark_return: Decimal | None = None,
    excess_return: Decimal | None = None,
    net_return: Decimal | None = None,
    net_excess_return: Decimal | None = None,
    maximum_adverse_excursion: Decimal | None = None,
    maximum_favorable_excursion: Decimal | None = None,
    holding_period_drawdown: Decimal | None = None,
    realized_volatility: Decimal | None = None,
    entry_date: date | None = None,
    exit_date: date | None = None,
    limitation: str,
) -> EvaluationRow:
    """Copy candidate and outcome values into the immutable dataset shape."""
    return EvaluationRow(
        cutoff=candidate.cutoff,
        instrument_id=candidate.instrument_id,
        canonical_symbol=candidate.canonical_symbol,
        eligibility=candidate.eligibility,
        score=candidate.score,
        rank=candidate.rank,
        universe_percentile=candidate.universe_percentile,
        relative_strength_120_percentile=candidate.relative_strength_120_percentile,
        tier=candidate.tier,
        evidence_completeness=candidate.evidence_completeness,
        horizon_sessions=horizon,
        outcome_status=outcome_status,
        absolute_return=absolute_return,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        net_return=net_return,
        net_excess_return=net_excess_return,
        maximum_adverse_excursion=maximum_adverse_excursion,
        maximum_favorable_excursion=maximum_favorable_excursion,
        holding_period_drawdown=holding_period_drawdown,
        realized_volatility=realized_volatility,
        entry_date=entry_date,
        exit_date=exit_date,
        limitation=limitation,
    )
