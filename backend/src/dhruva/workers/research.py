"""Inspect frozen research or compute technical candidates without a provider call.

``dhruva-research history`` reads only the local PostgreSQL observation ledger.
It never resolves a watchlist, rebuilds a score, polls news or requests market
data; the point is to inspect what was frozen then, not reinterpret it now.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, cast

from dhruva.contexts.analytics.api import (
    TECHNICAL_FEATURE_REVISION,
    BenchmarkBasis,
    FeatureStatus,
    TechnicalSeries,
    compute_technical_features,
    technical_series_from_daily_bars,
    unavailable_technical_features,
)
from dhruva.contexts.intelligence.application.candidate_evaluation import (
    build_evaluation_dataset,
    select_weekly_cutoffs,
)
from dhruva.contexts.intelligence.application.candidate_observations import (
    FreezeCandidateObservation,
    FreezeCandidateObservationCommand,
)
from dhruva.contexts.intelligence.application.candidate_outcomes import (
    MaterializeCandidateOutcomes,
    MaterializeCandidateOutcomesCommand,
)
from dhruva.contexts.intelligence.application.candidate_ranking import (
    CandidateInput,
    rank_technical_candidates,
)
from dhruva.contexts.intelligence.application.research_observations import (
    ListResearchObservations,
    ListResearchObservationsQuery,
)
from dhruva.contexts.intelligence.domain.candidates import CANDIDATE_RANKER_REVISION
from dhruva.contexts.intelligence.domain.model_evidence import (
    TECHNICAL_CANDIDATE_EVALUATION_REVISION,
    EvaluationDataset,
    EvaluationIdentity,
    EvaluationMetrics,
    EvaluationReadiness,
    UniverseType,
    build_evaluation_metrics,
    build_evaluation_readiness,
    evidence_export_bytes,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.marketdata.api import GetDailyBarSeries, GetDailyBarSeriesQuery
from dhruva.contexts.marketdata.infrastructure import SqlAlchemyMarketDataUnitOfWork
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.reference.api import GetSharedWatchlist
from dhruva.contexts.reference.infrastructure import SqlAlchemyReferenceUnitOfWork
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.shared.identity import InstrumentId
from dhruva.workers.cli_arguments import parse_account, parse_cutoff

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.intelligence.domain.attention import AttentionBand
    from dhruva.contexts.intelligence.domain.candidates import CandidateRanking, CandidateTier
    from dhruva.contexts.intelligence.domain.research_observation import (
        StoredResearchObservation,
    )
    from dhruva.shared.identity import AccountId

__all__ = ["build_parser", "main", "run"]

_EXIT_OK = 0
_EXIT_REFUSED = 2
_DEFAULT_LIMIT = 20
_DEFAULT_TOP = 5
_MAX_TOP = 20
_BENCHMARK_IDENTITY_KEY = "nse-index-nifty-50"
_LOOKBACK_DAYS = 730
_PRIMARY_HORIZON = 20
_SECONDARY_HORIZON = 60


def build_parser() -> argparse.ArgumentParser:
    """Build the local-only research-ledger command surface."""
    parser = argparse.ArgumentParser(
        prog="dhruva-research",
        description="Inspect immutable research observations stored in the local database.",
        epilog=(
            "Local database only: no Zerodha, GDELT or NSE request, no recommendation "
            "and no order. Candidate writes require explicit --freeze."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    history = subcommands.add_parser(
        "history",
        help="show the newest frozen attention observations",
    )
    history.add_argument("--account", required=True, help="account whose observations are read")
    history.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help=f"maximum observations to show (default {_DEFAULT_LIMIT}, maximum 100)",
    )
    history.add_argument(
        "--top",
        type=int,
        default=_DEFAULT_TOP,
        help=f"attention-bearing members to show per observation (default {_DEFAULT_TOP})",
    )
    candidates = subcommands.add_parser(
        "candidates",
        help="compute the experimental technical candidate baseline from local archives",
    )
    candidates.add_argument("--account", required=True, help="account whose watchlist is ranked")
    candidates.add_argument(
        "--as-of",
        help="PIT knowledge cutoff as ISO-8601 (default: current UTC time)",
    )
    candidates.add_argument(
        "--detail",
        action="store_true",
        help="show deterministic evidence and every excluded instrument",
    )
    candidates.add_argument(
        "--freeze",
        action="store_true",
        help="append this result as an official idempotent weekly observation",
    )
    outcomes = subcommands.add_parser(
        "outcomes",
        help="materialize mature 20/60-session outcomes for frozen candidates",
    )
    outcomes.add_argument("--account", required=True, help="account whose freezes mature")
    outcomes.add_argument("--as-of", help="latest locally observable UTC cutoff")
    outcomes.add_argument(
        "--cost-bps",
        type=_decimal_argument,
        default=Decimal("20"),
        help="explicit round-trip research cost in basis points (default 20)",
    )
    evidence = subcommands.add_parser(
        "evidence",
        help="show the prospective candidate observation/outcome evidence clock",
    )
    evidence.add_argument("--account", required=True, help="account whose evidence is read")
    evaluate = subcommands.add_parser(
        "evaluate",
        help="run a retrospective current-watchlist diagnostic from local bars",
    )
    evaluate.add_argument(
        "--account", required=True, help="account whose current watchlist is used"
    )
    evaluate.add_argument(
        "--model",
        default=CANDIDATE_RANKER_REVISION,
        help=f"frozen ranker revision (only {CANDIDATE_RANKER_REVISION})",
    )
    evaluate.add_argument("--from", dest="from_date", required=True, type=_date_argument)
    evaluate.add_argument("--to", dest="to_date", required=True, type=_date_argument)
    evaluate.add_argument(
        "--as-of",
        help="knowledge cutoff for reconstructed local bars (default current UTC time)",
    )
    evaluate.add_argument(
        "--strict-pit",
        action="store_true",
        help="rank only revisions and membership actually known at each historical cutoff",
    )
    evaluate.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=[20, 60],
        help="holding horizons in sessions (supported: 20 60)",
    )
    evaluate.add_argument(
        "--cost-bps",
        type=_decimal_argument,
        default=Decimal("20"),
        help="explicit round-trip research cost in basis points (default 20)",
    )
    evaluate.add_argument(
        "--export",
        type=Path,
        help="optional deterministic dhruva.model-evidence.v1 JSON output",
    )
    return parser


async def run(argv: Sequence[str] | None = None) -> int:
    """Run the selected local research query, disposing the engine afterward."""
    args = build_parser().parse_args(argv)
    account_id = parse_account(args.account)
    settings = load_settings()
    engine = build_engine(settings.db)
    session_factory = build_session_factory(engine)
    try:
        if args.command == "candidates":
            cutoff = parse_cutoff(args.as_of)
            ranking = await _build_candidates(
                account_id=account_id,
                cutoff=cutoff,
                session_factory=session_factory,
            )
            rendered = _render_candidates(ranking, detail=args.detail)
            if args.freeze:
                result = await FreezeCandidateObservation(
                    lambda account: SqlAlchemyIntelligenceUnitOfWork(
                        session_factory, account_id=account
                    )
                ).execute(
                    FreezeCandidateObservationCommand(
                        account_id=account_id,
                        ranking=ranking,
                        recorded_at=parse_cutoff(None),
                    )
                )
                action = "created" if result.created else "already present"
                rendered += (
                    f"\n\nOfficial candidate freeze: {action}; "
                    f"fingerprint {result.observation.observation_sha256}"
                )
            sys.stdout.write(rendered + "\n")
            return _EXIT_OK

        if args.command == "outcomes":
            cutoff = parse_cutoff(args.as_of)
            rendered = await _materialize_outcomes(
                account_id=account_id,
                observable_at=cutoff,
                cost_bps=args.cost_bps,
                session_factory=session_factory,
            )
            sys.stdout.write(rendered + "\n")
            return _EXIT_OK

        if args.command == "evidence":
            rendered = await _render_evidence_clock(
                account_id=account_id,
                session_factory=session_factory,
            )
            sys.stdout.write(rendered + "\n")
            return _EXIT_OK

        if args.command == "evaluate":
            if args.model != CANDIDATE_RANKER_REVISION:
                raise ValidationError(f"only frozen model {CANDIDATE_RANKER_REVISION} is supported")
            horizons = tuple(args.horizons)
            if not horizons or set(horizons) - {20, 60} or len(set(horizons)) != len(horizons):
                raise ValidationError("--horizons must contain unique values from: 20 60")
            if args.to_date < args.from_date:
                raise ValidationError("--to must not be earlier than --from")
            knowledge_cutoff = parse_cutoff(args.as_of)
            if args.to_date > knowledge_cutoff.date():
                raise ValidationError("--to cannot be later than the knowledge cutoff")
            dataset, metrics, readiness = await _evaluate(
                account_id=account_id,
                from_date=args.from_date,
                to_date=args.to_date,
                knowledge_cutoff=knowledge_cutoff,
                horizons=horizons,
                cost_bps=args.cost_bps,
                strict_pit=args.strict_pit,
                session_factory=session_factory,
            )
            if args.export is not None:
                args.export.write_bytes(evidence_export_bytes(dataset, metrics, readiness))
            sys.stdout.write(
                _render_evaluation(dataset, metrics, readiness, export_path=args.export) + "\n"
            )
            return _EXIT_OK

        if not 1 <= args.top <= _MAX_TOP:
            raise ValidationError(f"research history top count must be between 1 and {_MAX_TOP}")
        rows = await ListResearchObservations(
            lambda account: SqlAlchemyIntelligenceUnitOfWork(
                session_factory,
                account_id=account,
            )
        ).execute(ListResearchObservationsQuery(account_id=account_id, limit=args.limit))
        sys.stdout.write(_render_history(rows, top=args.top) + "\n")
        return _EXIT_OK
    finally:
        await engine.dispose()


async def _build_candidates(
    *,
    account_id: AccountId,
    cutoff: datetime,
    session_factory: async_sessionmaker[AsyncSession],
) -> CandidateRanking:
    """Resolve one PIT-local universe and its bars, then invoke the pure ranker."""

    def reference_uow(account: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)

    def marketdata_uow(account: AccountId) -> SqlAlchemyMarketDataUnitOfWork:
        return SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)

    def intelligence_uow(account: AccountId) -> SqlAlchemyIntelligenceUnitOfWork:
        return SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)

    watchlist = await GetSharedWatchlist(reference_uow).execute(
        account_id=account_id,
        effective_on=cutoff.date(),
        known_at=cutoff,
    )
    history = GetDailyBarSeries(marketdata_uow)
    from_date = cutoff.date() - timedelta(days=_LOOKBACK_DAYS)
    benchmark_id = InstrumentId.deterministic("reference", _BENCHMARK_IDENTITY_KEY)
    benchmark = None
    try:
        stored_benchmark = await history.execute(
            GetDailyBarSeriesQuery(
                account_id=account_id,
                instrument_id=benchmark_id,
                from_date=from_date,
                to_date=cutoff.date(),
                known_at=cutoff,
            )
        )
        benchmark = technical_series_from_daily_bars(stored_benchmark)
    except DhruvaError:
        pass

    observations = await ListResearchObservations(intelligence_uow).execute(
        ListResearchObservationsQuery(account_id=account_id, limit=100)
    )
    attention = next(
        (item.observation for item in observations if item.observation.cutoff <= cutoff), None
    )
    attention_by_id = (
        {item.instrument_id: item for item in attention.members} if attention is not None else {}
    )
    inputs: list[CandidateInput] = []
    for item in watchlist:
        identity = item.identity
        try:
            stored = await history.execute(
                GetDailyBarSeriesQuery(
                    account_id=account_id,
                    instrument_id=identity.instrument_id,
                    from_date=from_date,
                    to_date=cutoff.date(),
                    known_at=cutoff,
                )
            )
            features = compute_technical_features(
                stock=technical_series_from_daily_bars(stored),
                benchmark=benchmark,
                cutoff=cutoff,
                benchmark_basis=BenchmarkBasis.PRICE_INDEX,
            )
        except DhruvaError as error:
            features = unavailable_technical_features(
                instrument_id=identity.instrument_id,
                cutoff=cutoff,
                status=FeatureStatus.DATA_UNAVAILABLE,
                reason=str(error),
            )
        prior = attention_by_id.get(identity.instrument_id)
        inputs.append(
            CandidateInput(
                instrument_id=identity.instrument_id,
                canonical_symbol=identity.canonical_symbol,
                company_name=identity.company_name,
                features=features,
                attention_score=None if prior is None else prior.score,
                attention_band=None if prior is None else prior.band,
                attention_cutoff=None if attention is None or prior is None else attention.cutoff,
            )
        )
    return rank_technical_candidates(tuple(inputs), cutoff=cutoff)


async def _evaluate(  # noqa: PLR0913 - every parameter is an explicit methodology input
    *,
    account_id: AccountId,
    from_date: date,
    to_date: date,
    knowledge_cutoff: datetime,
    horizons: tuple[int, ...],
    cost_bps: Decimal,
    strict_pit: bool,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[EvaluationDataset, EvaluationMetrics, EvaluationReadiness]:
    """Replay the exact frozen ranker over today's watchlist, visibly biased."""

    def reference_uow(account: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account)

    def marketdata_uow(account: AccountId) -> SqlAlchemyMarketDataUnitOfWork:
        return SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)

    universe = await GetSharedWatchlist(reference_uow).execute(
        account_id=account_id,
        effective_on=knowledge_cutoff.date(),
        known_at=knowledge_cutoff,
    )
    history = GetDailyBarSeries(marketdata_uow)
    start = from_date - timedelta(days=900)
    benchmark_id = InstrumentId.deterministic("reference", _BENCHMARK_IDENTITY_KEY)
    try:
        stored_benchmark = await history.execute(
            GetDailyBarSeriesQuery(
                account_id=account_id,
                instrument_id=benchmark_id,
                from_date=start,
                to_date=knowledge_cutoff.date(),
                known_at=knowledge_cutoff,
            )
        )
    except DhruvaError as error:
        raise ValidationError(
            "evaluation is unsupported without locally observable NIFTY 50 history"
        ) from error
    benchmark = technical_series_from_daily_bars(stored_benchmark)
    stocks: dict[InstrumentId, TechnicalSeries] = {}
    for member in universe:
        try:
            stored = await history.execute(
                GetDailyBarSeriesQuery(
                    account_id=account_id,
                    instrument_id=member.identity.instrument_id,
                    from_date=start,
                    to_date=knowledge_cutoff.date(),
                    known_at=knowledge_cutoff,
                )
            )
            stocks[member.identity.instrument_id] = technical_series_from_daily_bars(stored)
        except DhruvaError:
            continue

    cutoff_dates = select_weekly_cutoffs(
        tuple(bar.trading_date for bar in benchmark.bars),
        from_date=from_date,
        to_date=to_date,
    )
    rankings: list[CandidateRanking] = []
    for day in cutoff_dates:
        cutoff = datetime.combine(day, time(10), tzinfo=UTC)
        if strict_pit:
            rankings.append(
                await _build_candidates(
                    account_id=account_id,
                    cutoff=cutoff,
                    session_factory=session_factory,
                )
            )
            continue
        inputs: list[CandidateInput] = []
        for member in universe:
            member_identity = member.identity
            stock = stocks.get(member_identity.instrument_id)
            features = (
                unavailable_technical_features(
                    instrument_id=member_identity.instrument_id,
                    cutoff=cutoff,
                    status=FeatureStatus.DATA_UNAVAILABLE,
                    reason="stock daily history is unavailable at the evaluation knowledge cutoff",
                )
                if stock is None
                else compute_technical_features(
                    stock=stock,
                    benchmark=benchmark,
                    cutoff=cutoff,
                    benchmark_basis=BenchmarkBasis.PRICE_INDEX,
                )
            )
            inputs.append(
                CandidateInput(
                    instrument_id=member_identity.instrument_id,
                    canonical_symbol=member_identity.canonical_symbol,
                    company_name=member_identity.company_name,
                    features=features,
                )
            )
        rankings.append(rank_technical_candidates(tuple(inputs), cutoff=cutoff))
    ranked_ids = {entry.instrument_id for ranking in rankings for entry in ranking.entries}
    for instrument_id in sorted(ranked_ids - stocks.keys(), key=str):
        try:
            stored = await history.execute(
                GetDailyBarSeriesQuery(
                    account_id=account_id,
                    instrument_id=instrument_id,
                    from_date=start,
                    to_date=knowledge_cutoff.date(),
                    known_at=knowledge_cutoff,
                )
            )
            stocks[instrument_id] = technical_series_from_daily_bars(stored)
        except DhruvaError:
            continue
    universe_type = (
        UniverseType.HISTORICAL_PIT_OWNER_WATCHLIST
        if strict_pit
        else UniverseType.RETROSPECTIVE_CURRENT_WATCHLIST
    )
    universe_label = (
        "PIT OWNER WATCHLIST" if strict_pit else "CURRENT OWNER WATCHLIST (RETROSPECTIVE)"
    )
    selection_limitation = (
        "owner watchlist membership is PIT-resolved but remains an owner-selected, "
        "non-survivorship-safe evaluation universe"
        if strict_pit
        else "historical bars were reconstructed after their market dates, not observed by "
        "DHRUVA prospectively at those ranking cutoffs"
    )
    evaluation_identity = EvaluationIdentity(
        ranker_revision=CANDIDATE_RANKER_REVISION,
        feature_revision=TECHNICAL_FEATURE_REVISION,
        evaluation_revision=TECHNICAL_CANDIDATE_EVALUATION_REVISION,
        benchmark_symbol="NIFTY 50",
        benchmark_basis=BenchmarkBasis.PRICE_INDEX.value,
        universe_label=universe_label,
        universe_type=universe_type,
        from_cutoff=from_date,
        to_cutoff=to_date,
        cadence="WEEKLY_FINAL_OBSERVED_TRADING_SESSION",
        horizons=horizons,
        execution_timing=(
            "ranking after completed session T; hypothetical entry next available session"
        ),
        entry_price_basis="NEXT_SESSION_OPEN",
        exit_price_basis="NTH_HOLDING_SESSION_CLOSE",
        cost_bps=cost_bps,
        knowledge_cutoff=knowledge_cutoff,
        limitations=(
            "RETROSPECTIVE DIAGNOSTIC ONLY — current-watchlist selection introduces "
            "survivorship/selection bias and cannot establish historical model efficacy",
            selection_limitation,
            "Zerodha corporate-action adjustment semantics are UNKNOWN",
            "NIFTY 50 is PRICE_INDEX, not TRI; dividends are excluded",
            "fundamentals, news, and attention are absent from candidate score and baselines",
        ),
    )
    observable_through = benchmark.bars[-1].trading_date
    dataset = build_evaluation_dataset(
        identity=evaluation_identity,
        rankings=tuple(rankings),
        stocks=stocks,
        benchmark=benchmark,
        observable_through=observable_through,
    )
    metrics = build_evaluation_metrics(dataset)
    adjustment_states = {series.adjustment_status.value for series in stocks.values()}
    adjustment = next(iter(adjustment_states)) if len(adjustment_states) == 1 else "MIXED"
    readiness = build_evaluation_readiness(
        dataset,
        sessions_available=len(benchmark.bars),
        benchmark_available=True,
        adjustment_semantics=adjustment,
        corporate_action_semantics="UNAVAILABLE",
    )
    return dataset, metrics, readiness


async def _materialize_outcomes(
    *,
    account_id: AccountId,
    observable_at: datetime,
    cost_bps: Decimal,
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Load only local PIT-visible series and advance the evidence clock."""
    async with SqlAlchemyIntelligenceUnitOfWork(
        session_factory, account_id=account_id
    ) as unit_of_work:
        observations = await unit_of_work.candidate_observations.list_recent(limit=10_000)
    visible = tuple(item for item in observations if item.observation.recorded_at <= observable_at)
    if not visible:
        return "No candidate-ranking observations are observable at this cutoff."

    def marketdata_uow(account: AccountId) -> SqlAlchemyMarketDataUnitOfWork:
        return SqlAlchemyMarketDataUnitOfWork(session_factory, account_id=account)

    earliest = min(item.observation.ranking.cutoff.date() for item in visible)
    instrument_ids = {
        candidate.instrument_id
        for item in visible
        for candidate in item.observation.ranking.entries
    }
    history = GetDailyBarSeries(marketdata_uow)
    benchmark_id = InstrumentId.deterministic("reference", _BENCHMARK_IDENTITY_KEY)
    benchmark = None
    benchmark_revisions: dict[date, str] = {}
    try:
        stored_benchmark = await history.execute(
            GetDailyBarSeriesQuery(
                account_id=account_id,
                instrument_id=benchmark_id,
                from_date=earliest,
                to_date=observable_at.date(),
                known_at=observable_at,
            )
        )
        benchmark = technical_series_from_daily_bars(stored_benchmark)
        benchmark_revisions = {
            bar.candle.trading_date: bar.source_revision for bar in stored_benchmark.bars
        }
    except DhruvaError:
        pass
    stocks: dict[InstrumentId, TechnicalSeries] = {}
    revisions: dict[InstrumentId, dict[date, str]] = {}
    for instrument_id in sorted(instrument_ids, key=str):
        try:
            stored = await history.execute(
                GetDailyBarSeriesQuery(
                    account_id=account_id,
                    instrument_id=instrument_id,
                    from_date=earliest,
                    to_date=observable_at.date(),
                    known_at=observable_at,
                )
            )
            stocks[instrument_id] = technical_series_from_daily_bars(stored)
            revisions[instrument_id] = {
                bar.candle.trading_date: bar.source_revision for bar in stored.bars
            }
        except DhruvaError:
            continue
    result = await MaterializeCandidateOutcomes(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(session_factory, account_id=account)
    ).execute(
        MaterializeCandidateOutcomesCommand(
            account_id=account_id,
            stocks=stocks,
            stock_bar_revisions=revisions,
            benchmark=benchmark,
            benchmark_bar_revisions=benchmark_revisions,
            observable_through=observable_at.date(),
            materialized_at=observable_at,
            cost_bps=cost_bps,
        )
    )
    return "\n".join(
        (
            "Candidate prospective outcome materialization (local data only)",
            f"Observations inspected: {result.observations}",
            f"Eligible frozen members: {result.eligible_members}",
            f"Mature outcomes: {result.mature_outcomes}",
            f"Created: {result.outcomes_created}; already present: "
            f"{result.outcomes_already_present}",
            f"Awaiting future sessions: {result.awaiting_outcomes}",
            f"Unavailable/degraded: {result.unavailable_outcomes}",
            "Execution: next-session open to Nth-session close; paper research only.",
            "Benchmark: NIFTY 50 PRICE_INDEX. Adjustment semantics remain UNKNOWN.",
        )
    )


async def _render_evidence_clock(
    *,
    account_id: AccountId,
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Summarize frozen candidates and already materialized outcomes."""
    async with SqlAlchemyIntelligenceUnitOfWork(
        session_factory, account_id=account_id
    ) as unit_of_work:
        observations = await unit_of_work.candidate_observations.list_recent(limit=10_000)
        outcomes = await unit_of_work.candidate_outcomes.list_all()
    expected = {
        (item.id, candidate.instrument_id, horizon)
        for item in observations
        for candidate in item.observation.ranking.entries
        if candidate.rank is not None
        for horizon in (20, 60)
    }
    mature = {
        (item.candidate_observation_id, item.instrument_id, item.horizon_sessions)
        for item in outcomes
    }
    return "\n".join(
        (
            "DHRUVA candidate prospective evidence clock",
            f"Frozen candidate observations: {len(observations)}",
            f"Expected eligible member/horizon outcomes: {len(expected)}",
            f"Mature 20-session outcomes: {sum(item[2] == _PRIMARY_HORIZON for item in mature)}",
            f"Mature 60-session outcomes: {sum(item[2] == _SECONDARY_HORIZON for item in mature)}",
            f"Awaiting or unavailable outcomes: {len(expected - mature)}",
            "Run `dhruva-research outcomes --account <account>` to classify and materialize "
            "locally observable outcomes.",
            "Frozen rankings remain immutable; outcomes are separate append-only facts.",
        )
    )


def _render_evaluation(
    dataset: EvaluationDataset,
    metrics: EvaluationMetrics,
    readiness: EvaluationReadiness,
    *,
    export_path: Path | None,
) -> str:
    """Render a qualified nontechnical model-evidence diagnostic."""
    identity = dataset.identity
    selection_warning = (
        "RETROSPECTIVE DIAGNOSTIC ONLY — current-watchlist selection introduces "
        "survivorship/selection bias and cannot establish historical model efficacy."
        if identity.universe_type is UniverseType.RETROSPECTIVE_CURRENT_WATCHLIST
        else "DIAGNOSTIC ONLY — PIT owner-watchlist membership remains owner-selected and "
        "not survivorship-safe, so it cannot establish historical model efficacy."
    )
    lines = [
        "DHRUVA technical candidate model evidence",
        selection_warning,
        f"Model: {identity.ranker_revision}; features: {identity.feature_revision}; "
        f"evaluation: {identity.evaluation_revision}",
        f"Universe: {identity.universe_label} ({identity.universe_type.value})",
        f"Window: {identity.from_cutoff} to {identity.to_cutoff}; cadence: {identity.cadence}",
        f"Benchmark: {identity.benchmark_symbol} {identity.benchmark_basis}",
        f"Execution: {identity.entry_price_basis} to {identity.exit_price_basis}; "
        f"round-trip cost {identity.cost_bps} bps",
        f"Readiness: {readiness.status.value}; sessions {readiness.sessions_available}; "
        f"ranking periods {readiness.ranking_periods}",
        f"Mature rows: 20-session {readiness.mature_20_outcomes}; "
        f"60-session {readiness.mature_60_outcomes}",
    ]
    lines.extend(f"Why: {reason}" for reason in readiness.reasons)
    lines.append("")
    lines.append("Cross-sectional rank quality (Spearman score vs future excess return):")
    for cross_metric in metrics.cross_sectional:
        lines.append(
            f"- {cross_metric.horizon_sessions} sessions: observations "
            f"{cross_metric.observation_count}; dates {cross_metric.ranking_dates}; "
            f"mean IC {_metric(cross_metric.mean_ic)}; "
            f"median {_metric(cross_metric.median_ic)}; positive periods "
            f"{_percent_metric(cross_metric.positive_period_proportion)}"
        )
    lines.append("Top-K equal-weight research cohorts:")
    for top_metric in metrics.top_k:
        lines.append(
            f"- {top_metric.horizon_sessions}s top {top_metric.k}: "
            f"n={top_metric.observation_count}, "
            f"gross excess {_percent_metric(top_metric.mean_gross_excess_return)}, "
            f"net excess {_percent_metric(top_metric.mean_net_excess_return)}, "
            f"hit rate {_percent_metric(top_metric.positive_excess_hit_rate)}, "
            f"worst {_percent_metric(top_metric.worst_excess_return)}, "
            f"best {_percent_metric(top_metric.best_excess_return)}"
        )
    lines.append("Rank buckets (1 is highest ranked):")
    lines.extend(
        f"- {item.horizon_sessions}s {item.group}: n={item.observation_count}, "
        f"mean excess {_percent_metric(item.mean_excess_return)}"
        for item in metrics.buckets
    )
    lines.append("Tier outcomes:")
    lines.extend(
        f"- {item.horizon_sessions}s {item.group}: n={item.observation_count}, "
        f"mean excess {_percent_metric(item.mean_excess_return)}"
        for item in metrics.tiers
    )
    lines.append("Transparent baselines:")
    lines.extend(
        f"- {item.horizon_sessions}s {item.baseline}: n={item.observation_count}, "
        f"mean return {_percent_metric(item.mean_return)}, "
        f"mean excess {_percent_metric(item.mean_excess_return)}"
        for item in metrics.baselines
    )
    lines.append("")
    lines.extend(f"Limitation: {item}" for item in identity.limitations)
    lines.append(
        "Conclusion: useful retrospective diagnostic, but insufficient for credible "
        "historical validation. This output is not a recommendation or BUY/SELL instruction."
    )
    if export_path is not None:
        lines.append(f"Deterministic export: {export_path}")
    return "\n".join(lines)


def _metric(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value.quantize(Decimal("0.0001")))


def _percent_metric(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{(value * Decimal(100)).quantize(Decimal('0.01'))}%"


def _date_argument(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _decimal_argument(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError("expected a decimal number") from error
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative finite decimal")
    return value


def _render_candidates(ranking: CandidateRanking, *, detail: bool) -> str:
    """Render a compact experimental result without recommendation language."""
    lines = [
        f"Research candidates — {ranking.experimental_label}",
        f"As of: {ranking.cutoff.isoformat()}",
        f"Universe: {ranking.universe_label} ({len(ranking.entries)} instruments)",
        f"Ranker: {ranking.ranker_revision}; features: {ranking.feature_revision}",
        f"Benchmark: {ranking.benchmark_symbol} {ranking.benchmark_basis}",
    ]
    lines.extend(f"Limitation: {item}" for item in ranking.limitations)
    lines.append("")
    eligible = tuple(item for item in ranking.entries if item.rank is not None)
    if not eligible:
        lines.append(
            "No instrument has the essential history required for a normal candidate rank."
        )
    for item in eligible:
        tier = cast("CandidateTier", item.tier)
        lines.append(
            f"{item.rank}. {item.canonical_symbol}  score {item.score}  {tier.value}  "
            f"evidence {item.evidence_completeness.value}"
        )
        if item.attention_score is not None:
            attention_band = cast("AttentionBand", item.attention_band)
            lines.append(f"   separate attention: {item.attention_score} {attention_band.value}")
        if detail:
            lines.extend(f"   supports: {reason}" for reason in item.supporting_evidence)
            lines.extend(f"   counters: {reason}" for reason in item.counterevidence)
            lines.extend(f"   missing: {reason}" for reason in item.missing_evidence)
    excluded = tuple(item for item in ranking.entries if item.rank is None)
    if excluded:
        lines.append("")
        lines.append(f"Excluded ({len(excluded)}):")
        shown = excluded if detail else excluded[:5]
        lines.extend(f"- {item.canonical_symbol}: {item.eligibility.value}" for item in shown)
        if len(shown) < len(excluded):
            lines.append(f"- … {len(excluded) - len(shown)} more; use --detail")
    return "\n".join(lines)


def _render_history(rows: Sequence[StoredResearchObservation], *, top: int) -> str:
    """Render compact immutable facts without turning attention into advice."""
    lines = [
        "DHRUVA research observation history",
        "ATTENTION_OBSERVATION records unusual observable activity; it is not a recommendation.",
        "",
    ]
    if not rows:
        lines.append("No research observations have been frozen for this account.")
        return "\n".join(lines)

    for stored in rows:
        observation = stored.observation
        lines.extend(
            (
                f"{observation.cutoff.isoformat()}  {observation.observation_type.value}",
                f"  status: {observation.status.value}  ranked: {len(observation.members)}  "
                f"attention-bearing: {observation.attention_count}",
                f"  fingerprint: {observation.observation_sha256}",
                f"  packet body: {observation.packet_body_sha256}",
            )
        )
        if stored.supersedes_sha256 is not None:
            lines.append(f"  supersedes: {stored.supersedes_sha256}")
        health = observation.source_health
        lines.append(f"  sources: market {health.market.value}; news {health.news.value}")
        lines.extend(f"    degraded: {reason}" for reason in health.degraded_reasons)
        noteworthy = tuple(member for member in observation.members if member.score > 0)[:top]
        if noteworthy:
            lines.append(
                "  top attention: "
                + ", ".join(
                    f"{member.rank}. {member.canonical_symbol} {member.band.value} ({member.score})"
                    for member in noteworthy
                )
            )
        else:
            lines.append("  top attention: none; the full quiet universe was still preserved")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return asyncio.run(run(argv))
    except (ValidationError, DhruvaError) as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
