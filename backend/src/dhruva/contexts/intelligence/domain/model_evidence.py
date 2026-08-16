"""Versioned, deterministic evidence vocabulary and evaluation mathematics."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "MODEL_EVIDENCE_SCHEMA",
    "TECHNICAL_CANDIDATE_EVALUATION_REVISION",
    "BaselineMetric",
    "CrossSectionalMetric",
    "EvaluationDataset",
    "EvaluationIdentity",
    "EvaluationMetrics",
    "EvaluationReadiness",
    "EvaluationRow",
    "EvidenceStatus",
    "GroupMetric",
    "TopKMetric",
    "UniverseType",
    "build_evaluation_metrics",
    "build_evaluation_readiness",
    "evidence_export_bytes",
]

TECHNICAL_CANDIDATE_EVALUATION_REVISION: Final = "technical-candidate-evaluation-v0"
MODEL_EVIDENCE_SCHEMA: Final = "dhruva.model-evidence.v1"
_SERIOUS_HISTORY_SESSIONS = 2_000
_MIN_RANKING_PERIODS = 104
_MIN_MATURE_ROWS_PER_HORIZON = 100
_MIN_BUCKET_COUNT = 2
_MIN_IC_OBSERVATIONS = 2
_PRIMARY_HORIZON = 20
_SECONDARY_HORIZON = 60


class UniverseType(StrEnum):
    """The provenance class of securities included in an evaluation."""

    RETROSPECTIVE_CURRENT_WATCHLIST = "RETROSPECTIVE_CURRENT_WATCHLIST"
    HISTORICAL_PIT_OWNER_WATCHLIST = "HISTORICAL_PIT_OWNER_WATCHLIST"
    HISTORICAL_PIT_UNIVERSE = "HISTORICAL_PIT_UNIVERSE"
    PROSPECTIVE_FROZEN = "PROSPECTIVE_FROZEN"


class EvidenceStatus(StrEnum):
    """Whether the persisted facts can support model-efficacy claims."""

    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    EVALUATION_READY = "EVALUATION_READY"
    DEGRADED = "DEGRADED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    """Every immutable identity and assumption attached to an evaluation."""

    ranker_revision: str
    feature_revision: str
    evaluation_revision: str
    benchmark_symbol: str
    benchmark_basis: str
    universe_label: str
    universe_type: UniverseType
    from_cutoff: date
    to_cutoff: date
    cadence: str
    horizons: tuple[int, ...]
    execution_timing: str
    entry_price_basis: str
    exit_price_basis: str
    cost_bps: Decimal
    knowledge_cutoff: datetime
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject anonymous, reversed, or implicit evaluation semantics."""
        for revision in (self.ranker_revision, self.feature_revision, self.evaluation_revision):
            invariant(revision.strip() != "", "evaluation revision cannot be blank")
        invariant(self.from_cutoff <= self.to_cutoff, "evaluation date range is reversed")
        invariant(bool(self.horizons) and all(item > 0 for item in self.horizons), "bad horizons")
        invariant(len(set(self.horizons)) == len(self.horizons), "evaluation horizon repeated")
        invariant(self.cost_bps >= 0, "evaluation cost cannot be negative")


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    """One frozen rank member joined to one horizon's later outcome."""

    cutoff: datetime
    instrument_id: InstrumentId
    canonical_symbol: str
    eligibility: CandidateEligibility
    score: Decimal | None
    rank: int | None
    universe_percentile: Decimal | None
    relative_strength_120_percentile: Decimal | None
    tier: CandidateTier | None
    evidence_completeness: EvidenceCompleteness
    horizon_sessions: int
    outcome_status: str
    absolute_return: Decimal | None
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    net_return: Decimal | None
    net_excess_return: Decimal | None
    maximum_adverse_excursion: Decimal | None
    maximum_favorable_excursion: Decimal | None
    holding_period_drawdown: Decimal | None
    realized_volatility: Decimal | None
    entry_date: date | None
    exit_date: date | None
    limitation: str

    @property
    def mature(self) -> bool:
        """Return whether every return metric is available."""
        return self.outcome_status == "MATURE"


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """Deterministically ordered evaluation rows plus full methodology identity."""

    identity: EvaluationIdentity
    rows: tuple[EvaluationRow, ...]

    def __post_init__(self) -> None:
        """Require a stable row order with one row per member/horizon/cutoff."""
        keys = tuple(
            (
                row.cutoff,
                row.horizon_sessions,
                row.rank is None,
                row.rank or 0,
                row.canonical_symbol,
            )
            for row in self.rows
        )
        invariant(keys == tuple(sorted(keys)), "evaluation rows are not deterministic")
        logical = tuple((row.cutoff, row.instrument_id, row.horizon_sessions) for row in self.rows)
        invariant(len(logical) == len(set(logical)), "evaluation row repeated")


@dataclass(frozen=True, slots=True)
class CrossSectionalMetric:
    """Distribution of per-ranking-date Spearman rank IC values."""

    horizon_sessions: int
    observation_count: int
    ranking_dates: int
    mean_ic: Decimal | None
    median_ic: Decimal | None
    standard_deviation: Decimal | None
    positive_period_proportion: Decimal | None


@dataclass(frozen=True, slots=True)
class TopKMetric:
    """Equal-weight top-K paper cohort outcomes across ranking dates."""

    horizon_sessions: int
    k: int
    cohort_periods: int
    observation_count: int
    mean_gross_return: Decimal | None
    mean_benchmark_return: Decimal | None
    mean_gross_excess_return: Decimal | None
    mean_net_return: Decimal | None
    mean_net_excess_return: Decimal | None
    positive_excess_hit_rate: Decimal | None
    median_excess_return: Decimal | None
    excess_dispersion: Decimal | None
    worst_excess_return: Decimal | None
    best_excess_return: Decimal | None
    worst_holding_drawdown: Decimal | None


@dataclass(frozen=True, slots=True)
class GroupMetric:
    """Outcome distribution for a deterministic tier or rank bucket."""

    horizon_sessions: int
    group: str
    observation_count: int
    mean_excess_return: Decimal | None
    median_excess_return: Decimal | None
    dispersion: Decimal | None
    positive_excess_hit_rate: Decimal | None


@dataclass(frozen=True, slots=True)
class BaselineMetric:
    """Transparent comparison series available from the same frozen rows."""

    horizon_sessions: int
    baseline: str
    observation_count: int
    mean_return: Decimal | None
    mean_excess_return: Decimal | None


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    """Complete deterministic metric families for one dataset."""

    cross_sectional: tuple[CrossSectionalMetric, ...]
    top_k: tuple[TopKMetric, ...]
    buckets: tuple[GroupMetric, ...]
    tiers: tuple[GroupMetric, ...]
    baselines: tuple[BaselineMetric, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReadiness:
    """Evidence sufficiency stated separately from feature readiness."""

    sessions_available: int
    requested_warmup_sessions: int
    evaluation_span_sessions: int
    universe_type: UniverseType
    benchmark_available: bool
    adjustment_semantics: str
    corporate_action_semantics: str
    survivorship_safe: bool
    ranking_periods: int
    mature_20_outcomes: int
    mature_60_outcomes: int
    status: EvidenceStatus
    reasons: tuple[str, ...]


def build_evaluation_metrics(
    dataset: EvaluationDataset,
    *,
    top_ks: tuple[int, ...] = (1, 3, 5),
    bucket_count: int = 5,
) -> EvaluationMetrics:
    """Compute IC, cohorts, monotonicity, tier, and transparent baselines."""
    invariant(bucket_count >= _MIN_BUCKET_COUNT, "evaluation needs at least two rank buckets")
    cross: list[CrossSectionalMetric] = []
    top: list[TopKMetric] = []
    buckets: list[GroupMetric] = []
    tiers: list[GroupMetric] = []
    baselines: list[BaselineMetric] = []
    for horizon in dataset.identity.horizons:
        mature = tuple(
            row
            for row in dataset.rows
            if row.horizon_sessions == horizon
            and row.mature
            and row.rank is not None
            and row.score is not None
            and row.excess_return is not None
        )
        periods = _by_cutoff(mature)
        ics = tuple(ic for rows in periods.values() if (ic := _spearman(rows)) is not None)
        cross.append(
            CrossSectionalMetric(
                horizon_sessions=horizon,
                observation_count=sum(len(rows) for rows in periods.values()),
                ranking_dates=len(ics),
                mean_ic=_mean(ics),
                median_ic=_median(ics),
                standard_deviation=_std(ics),
                positive_period_proportion=_proportion_positive(ics),
            )
        )
        for k in top_ks:
            top.append(_top_k(periods, horizon=horizon, k=k))
        for group, rows in _bucket_rows(periods, bucket_count=bucket_count).items():
            buckets.append(_group_metric(horizon, f"RANK_BUCKET_{group}", rows))
        for tier in CandidateTier:
            rows = tuple(row for row in mature if row.tier is tier)
            tiers.append(_group_metric(horizon, tier.value, rows))
        baselines.extend(_baselines(mature, horizon=horizon))
    return EvaluationMetrics(
        cross_sectional=tuple(cross),
        top_k=tuple(top),
        buckets=tuple(buckets),
        tiers=tuple(tiers),
        baselines=tuple(baselines),
    )


def build_evaluation_readiness(  # noqa: PLR0913 - readiness gates are explicit evidence
    dataset: EvaluationDataset,
    *,
    sessions_available: int,
    requested_warmup_sessions: int = 252,
    benchmark_available: bool = True,
    adjustment_semantics: str = "UNKNOWN",
    corporate_action_semantics: str = "UNAVAILABLE",
) -> EvaluationReadiness:
    """Classify serious-evaluation readiness without conflating it with warm-up."""
    ranking_periods = len({row.cutoff for row in dataset.rows})
    mature20 = sum(row.mature and row.horizon_sessions == _PRIMARY_HORIZON for row in dataset.rows)
    mature60 = sum(
        row.mature and row.horizon_sessions == _SECONDARY_HORIZON for row in dataset.rows
    )
    survivorship_safe = dataset.identity.universe_type is UniverseType.HISTORICAL_PIT_UNIVERSE
    reasons: list[str] = []
    if not benchmark_available:
        status = EvidenceStatus.UNSUPPORTED
        reasons.append("benchmark history is unavailable")
    elif not dataset.rows or ranking_periods == 0:
        status = EvidenceStatus.INSUFFICIENT_DATA
        reasons.append("no ranking periods were generated")
    elif not survivorship_safe:
        status = EvidenceStatus.DIAGNOSTIC_ONLY
        reasons.append("current-watchlist selection is not survivorship safe")
    elif adjustment_semantics not in {"ADJUSTED", "VERIFIED"}:
        status = EvidenceStatus.DEGRADED
        reasons.append("corporate-action adjustment semantics are not verified")
    elif (
        sessions_available < _SERIOUS_HISTORY_SESSIONS
        or ranking_periods < _MIN_RANKING_PERIODS
        or mature20 < _MIN_MATURE_ROWS_PER_HORIZON
        or mature60 < _MIN_MATURE_ROWS_PER_HORIZON
    ):
        status = EvidenceStatus.INSUFFICIENT_DATA
        reasons.append(
            "history, ranking periods, or mature outcomes are below evaluation thresholds"
        )
    else:
        status = EvidenceStatus.EVALUATION_READY
        reasons.append("minimum depth, PIT universe, adjustment, and maturity gates are satisfied")
    if sessions_available < _SERIOUS_HISTORY_SESSIONS:
        reasons.append(
            f"{sessions_available} sessions available; serious validation target is "
            f"{_SERIOUS_HISTORY_SESSIONS}"
        )
    if adjustment_semantics == "UNKNOWN":
        reasons.append("price adjustment semantics are UNKNOWN")
    return EvaluationReadiness(
        sessions_available=sessions_available,
        requested_warmup_sessions=requested_warmup_sessions,
        evaluation_span_sessions=max(sessions_available - requested_warmup_sessions, 0),
        universe_type=dataset.identity.universe_type,
        benchmark_available=benchmark_available,
        adjustment_semantics=adjustment_semantics,
        corporate_action_semantics=corporate_action_semantics,
        survivorship_safe=survivorship_safe,
        ranking_periods=ranking_periods,
        mature_20_outcomes=mature20,
        mature_60_outcomes=mature60,
        status=status,
        reasons=tuple(reasons),
    )


def evidence_export_bytes(
    dataset: EvaluationDataset,
    metrics: EvaluationMetrics,
    readiness: EvaluationReadiness,
) -> bytes:
    """Return byte-deterministic canonical JSON for identical explicit inputs."""
    body = {
        "schema": MODEL_EVIDENCE_SCHEMA,
        "identity": _json_value(dataset.identity),
        "methodology": {
            "cadence": dataset.identity.cadence,
            "execution_timing": dataset.identity.execution_timing,
            "entry_price_basis": dataset.identity.entry_price_basis,
            "exit_price_basis": dataset.identity.exit_price_basis,
            "cost_bps": format(dataset.identity.cost_bps, "f"),
        },
        "metrics": _json_value(metrics),
        "readiness": _json_value(readiness),
        "rows": _json_value(dataset.rows),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    envelope = {
        "content_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "evidence": body,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (encoded + "\n").encode()


def _by_cutoff(rows: tuple[EvaluationRow, ...]) -> dict[datetime, tuple[EvaluationRow, ...]]:
    result: dict[datetime, list[EvaluationRow]] = {}
    for row in rows:
        result.setdefault(row.cutoff, []).append(row)
    return {
        cutoff: tuple(sorted(items, key=lambda item: (item.rank or 0, item.canonical_symbol)))
        for cutoff, items in sorted(result.items())
    }


def _spearman(rows: tuple[EvaluationRow, ...]) -> Decimal | None:
    if len(rows) < _MIN_IC_OBSERVATIONS:
        return None
    scores = {index: row.score for index, row in enumerate(rows)}
    outcomes = {index: row.excess_return for index, row in enumerate(rows)}
    if any(value is None for value in (*scores.values(), *outcomes.values())):
        return None
    score_ranks = _midranks({key: value for key, value in scores.items() if value is not None})
    outcome_ranks = _midranks({key: value for key, value in outcomes.items() if value is not None})
    keys = tuple(range(len(rows)))
    return _correlation(
        tuple(score_ranks[key] for key in keys),
        tuple(outcome_ranks[key] for key in keys),
    )


def _midranks(values: dict[int, Decimal]) -> dict[int, Decimal]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    result: dict[int, Decimal] = {}
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        rank = (Decimal(index + 1) + Decimal(end + 1)) / Decimal(2)
        for position in range(index, end + 1):
            result[ordered[position][0]] = rank
        index = end + 1
    return result


def _correlation(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> Decimal | None:
    left_mean = _mean(left)
    right_mean = _mean(right)
    if left_mean is None or right_mean is None:
        return None
    covariance = sum(
        ((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True)),
        Decimal(0),
    )
    left_variance = sum(((x - left_mean) ** 2 for x in left), Decimal(0))
    right_variance = sum(((y - right_mean) ** 2 for y in right), Decimal(0))
    if left_variance == 0 or right_variance == 0:
        return None
    with localcontext() as context:
        context.prec = 28
        return covariance / (left_variance * right_variance).sqrt()


def _top_k(
    periods: dict[datetime, tuple[EvaluationRow, ...]], *, horizon: int, k: int
) -> TopKMetric:
    cohorts = tuple(rows[:k] for rows in periods.values() if len(rows) >= k)
    selected = tuple(row for cohort in cohorts for row in cohort)
    excess = _values(selected, "excess_return")
    drawdowns = _values(selected, "holding_period_drawdown")
    return TopKMetric(
        horizon_sessions=horizon,
        k=k,
        cohort_periods=len(cohorts),
        observation_count=len(selected),
        mean_gross_return=_mean(_values(selected, "absolute_return")),
        mean_benchmark_return=_mean(_values(selected, "benchmark_return")),
        mean_gross_excess_return=_mean(excess),
        mean_net_return=_mean(_values(selected, "net_return")),
        mean_net_excess_return=_mean(_values(selected, "net_excess_return")),
        positive_excess_hit_rate=_proportion_positive(excess),
        median_excess_return=_median(excess),
        excess_dispersion=_std(excess),
        worst_excess_return=min(excess) if excess else None,
        best_excess_return=max(excess) if excess else None,
        worst_holding_drawdown=min(drawdowns) if drawdowns else None,
    )


def _bucket_rows(
    periods: dict[datetime, tuple[EvaluationRow, ...]], *, bucket_count: int
) -> dict[int, tuple[EvaluationRow, ...]]:
    grouped: dict[int, list[EvaluationRow]] = {index: [] for index in range(1, bucket_count + 1)}
    for rows in periods.values():
        size = len(rows)
        for index, row in enumerate(rows):
            bucket = min(index * bucket_count // size + 1, bucket_count)
            grouped[bucket].append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _group_metric(horizon: int, group: str, rows: tuple[EvaluationRow, ...]) -> GroupMetric:
    values = _values(rows, "excess_return")
    return GroupMetric(
        horizon_sessions=horizon,
        group=group,
        observation_count=len(values),
        mean_excess_return=_mean(values),
        median_excess_return=_median(values),
        dispersion=_std(values),
        positive_excess_hit_rate=_proportion_positive(values),
    )


def _baselines(rows: tuple[EvaluationRow, ...], *, horizon: int) -> tuple[BaselineMetric, ...]:
    universe_returns = _values(rows, "absolute_return")
    benchmark_returns = _values(rows, "benchmark_return")
    momentum = tuple(
        row
        for period in _by_cutoff(rows).values()
        for row in sorted(
            (item for item in period if item.relative_strength_120_percentile is not None),
            key=_momentum_sort_key,
        )[:5]
    )
    return (
        BaselineMetric(
            horizon_sessions=horizon,
            baseline="BENCHMARK_PRICE_INDEX",
            observation_count=len(benchmark_returns),
            mean_return=_mean(benchmark_returns),
            mean_excess_return=Decimal(0) if benchmark_returns else None,
        ),
        BaselineMetric(
            horizon_sessions=horizon,
            baseline="EQUAL_WEIGHT_ELIGIBLE_UNIVERSE",
            observation_count=len(universe_returns),
            mean_return=_mean(universe_returns),
            mean_excess_return=_mean(_values(rows, "excess_return")),
        ),
        BaselineMetric(
            horizon_sessions=horizon,
            baseline="TOP5_120_SESSION_RELATIVE_MOMENTUM",
            observation_count=len(momentum),
            mean_return=_mean(_values(momentum, "absolute_return")),
            mean_excess_return=_mean(_values(momentum, "excess_return")),
        ),
    )


def _values(rows: tuple[EvaluationRow, ...], field: str) -> tuple[Decimal, ...]:
    values = (getattr(row, field) for row in rows)
    return tuple(value for value in values if isinstance(value, Decimal))


def _momentum_sort_key(row: EvaluationRow) -> tuple[Decimal, str]:
    value = row.relative_strength_120_percentile
    if value is None:  # pragma: no cover - caller filters unavailable momentum
        raise ValueError("momentum baseline received unavailable momentum")
    return -value, row.canonical_symbol


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    return None if not values else sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _std(values: tuple[Decimal, ...]) -> Decimal | None:
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
    with localcontext() as context:
        context.prec = 28
        return variance.sqrt()


def _proportion_positive(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return Decimal(sum(value > 0 for value in values)) / Decimal(len(values))


def _json_value(value: object) -> object:
    if is_dataclass(value):
        return _json_value(asdict(value))  # type: ignore[arg-type]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (Decimal, StrEnum)):
        return value.value if isinstance(value, StrEnum) else format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value.__class__.__name__.endswith("Id") else value
