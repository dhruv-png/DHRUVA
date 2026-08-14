"""Inspect frozen research or compute technical candidates without a provider call.

``dhruva-research history`` reads only the local PostgreSQL observation ledger.
It never resolves a watchlist, rebuilds a score, polls news or requests market
data; the point is to inspect what was frozen then, not reinterpret it now.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from dhruva.contexts.analytics.api import (
    BenchmarkBasis,
    FeatureStatus,
    compute_technical_features,
    technical_series_from_daily_bars,
    unavailable_technical_features,
)
from dhruva.contexts.intelligence.application.candidate_observations import (
    FreezeCandidateObservation,
    FreezeCandidateObservationCommand,
)
from dhruva.contexts.intelligence.application.candidate_ranking import (
    CandidateInput,
    rank_technical_candidates,
)
from dhruva.contexts.intelligence.application.research_observations import (
    ListResearchObservations,
    ListResearchObservationsQuery,
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
