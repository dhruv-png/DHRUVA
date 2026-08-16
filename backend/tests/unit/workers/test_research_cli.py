"""The research-history CLI is compact, local-only and semantically explicit."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dhruva.contexts.analytics.api import (
    AdjustmentEvidence,
    FeatureName,
    FeatureStatus,
    TechnicalBar,
    TechnicalSeries,
)
from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.contexts.intelligence.domain.candidates import CandidateEligibility, CandidateRanking
from dhruva.contexts.intelligence.domain.model_evidence import (
    EvaluationDataset,
    EvaluationIdentity,
    EvaluationMetrics,
    EvaluationReadiness,
    EvidenceStatus,
    UniverseType,
)
from dhruva.contexts.intelligence.domain.research_observation import (
    AttentionObservationMember,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationSourceStatus,
    ObservationType,
    ResearchObservation,
    StoredResearchObservation,
    observation_fingerprint,
    universe_fingerprint,
)
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.workers import research as cli

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("owner-family")
CUTOFF = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
INSTRUMENT = InstrumentId.deterministic("reference", "hal")


def _stored(*, degraded: bool = False) -> StoredResearchObservation:
    member = AttentionObservationMember(
        instrument_id=INSTRUMENT,
        rank=1,
        canonical_symbol="HAL",
        company_name="Hindustan Aeronautics Limited",
        score=3,
        band=AttentionBand.ELEVATED,
        reasons=("1-day absolute move 3%",),
        market_context_available=True,
        market_context_sha256="1" * 64,
        archived_news_revisions=(),
    )
    members = (member,)
    provenance = ObservationProvenance(
        attention_revision="watchlist-attention-v1",
        digest_revision="watchlist-digest-v1",
        digest_policy_revision="watchlist-digest-v1",
        entity_linking_revision="entity-linking-v1",
        event_classification_revision="event-classification-v1",
        market_context_revision="market-context-v1",
        news_identity_revision="news-identity-v1",
        sentiment_revision="lexical-sentiment-v1",
        packet_schema_revision="dhruva.research-packet.v1",
    )
    health = ObservationSourceHealth(
        market=ObservationSourceStatus.HEALTHY,
        news=ObservationSourceStatus.DEGRADED if degraded else ObservationSourceStatus.HEALTHY,
        degraded_reasons=("news: rate limited",) if degraded else (),
    )
    universe_sha = universe_fingerprint(members)
    packet_hash = "2" * 64
    fingerprint = observation_fingerprint(
        account_id=ACCOUNT,
        observation_type=ObservationType.ATTENTION_OBSERVATION,
        cutoff=CUTOFF,
        universe_sha256=universe_sha,
        packet_body_sha256=packet_hash,
        provenance=provenance,
        source_health=health,
        members=members,
    )
    return StoredResearchObservation(
        observation=ResearchObservation(
            account_id=ACCOUNT,
            observation_type=ObservationType.ATTENTION_OBSERVATION,
            cutoff=CUTOFF,
            recorded_at=CUTOFF,
            provenance=provenance,
            source_health=health,
            members=members,
            universe_sha256=universe_sha,
            observation_sha256=fingerprint,
            packet_body_sha256=packet_hash,
        )
    )


def test_history_requires_an_account_and_has_small_bounded_defaults() -> None:
    """There is no ambient tenant and no unbounded history read."""
    args = cli.build_parser().parse_args(["history", "--account", "owner-family"])

    assert args.command == "history"
    assert args.account == "owner-family"
    assert args.limit == 20
    assert args.top == 5
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["history"])


def test_render_names_attention_and_denies_recommendation_semantics() -> None:
    """The inspection surface cannot make attention look like advice."""
    rendered = cli._render_history((_stored(),), top=5)

    assert "ATTENTION_OBSERVATION" in rendered
    assert "not a recommendation" in rendered
    assert "HAL ELEVATED (3)" in rendered
    assert "packet body" in rendered
    assert "fingerprint" in rendered


def test_render_preserves_degraded_source_health() -> None:
    """An operator can see which source phase degraded the observation."""
    rendered = cli._render_history((_stored(degraded=True),), top=5)

    assert "status: DEGRADED" in rendered
    assert "news DEGRADED" in rendered
    assert "news: rate limited" in rendered


def test_empty_history_is_explicit() -> None:
    """No rows is stated as no evidence rather than an empty success screen."""
    assert "No research observations" in cli._render_history((), top=5)


def test_candidate_command_is_explicitly_local_experimental_and_optionally_frozen() -> None:
    """Manual inspection does not freeze unless the owner supplies the explicit flag."""
    args = cli.build_parser().parse_args(
        ["candidates", "--account", "owner-family", "--detail", "--freeze"]
    )
    ranking = CandidateRanking(
        cutoff=CUTOFF,
        entries=(),
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        feature_revision="technical-features-v0",
        limitations=("current watchlist is not survivorship-safe",),
    )

    rendered = cli._render_candidates(ranking, detail=args.detail)

    assert args.freeze is True
    assert "EXPERIMENTAL RESEARCH CANDIDATE" in rendered
    assert "PRICE_INDEX" in rendered
    assert "No instrument has the essential history" in rendered
    assert "not survivorship-safe" in rendered


def test_evaluation_and_outcome_commands_expose_explicit_methodology() -> None:
    """The local evidence surface requires dates and states costs rather than hiding them."""
    evaluate = cli.build_parser().parse_args(
        [
            "evaluate",
            "--account",
            "owner-family",
            "--from",
            "2025-01-01",
            "--to",
            "2026-01-01",
            "--horizons",
            "20",
            "60",
            "--cost-bps",
            "25",
            "--strict-pit",
        ]
    )
    outcomes = cli.build_parser().parse_args(
        ["outcomes", "--account", "owner-family", "--cost-bps", "20"]
    )
    evidence = cli.build_parser().parse_args(["evidence", "--account", "owner-family"])

    assert evaluate.model == "technical-candidate-v0"
    assert evaluate.horizons == [20, 60]
    assert evaluate.cost_bps == Decimal(25)
    assert evaluate.strict_pit is True
    assert evaluate.universe == "current-owner-watchlist"
    assert outcomes.cost_bps == Decimal(20)
    assert evidence.command == "evidence"


def test_historical_evaluation_universe_is_an_explicit_contract() -> None:
    """A named historical source is parsed distinctly and cannot imply fallback."""
    args = cli.build_parser().parse_args(
        [
            "evaluate",
            "--account",
            "owner-family",
            "--universe",
            "licensed-india-v1",
            "--from",
            "2018-01-01",
            "--to",
            "2025-01-01",
            "--strict-pit",
        ]
    )

    assert args.universe == "licensed-india-v1"
    assert args.strict_pit


def test_evaluation_report_names_the_exact_pit_universe_limitation() -> None:
    """Strict PIT output must not mislabel historical owner membership as today's universe."""
    identity = EvaluationIdentity(
        ranker_revision="technical-candidate-v0",
        feature_revision="technical-features-v0",
        evaluation_revision="technical-candidate-evaluation-v0",
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        universe_label="PIT OWNER WATCHLIST",
        universe_type=UniverseType.HISTORICAL_PIT_OWNER_WATCHLIST,
        from_cutoff=CUTOFF.date(),
        to_cutoff=CUTOFF.date(),
        cadence="WEEKLY_FINAL_SESSION",
        horizons=(20, 60),
        execution_timing="AFTER_SIGNAL_SESSION",
        entry_price_basis="NEXT_SESSION_OPEN",
        exit_price_basis="HOLDING_SESSION_N_CLOSE",
        cost_bps=Decimal(20),
        knowledge_cutoff=CUTOFF,
        limitations=("owner-selected universe",),
    )
    readiness = EvaluationReadiness(
        sessions_available=496,
        requested_warmup_sessions=252,
        evaluation_span_sessions=244,
        universe_type=identity.universe_type,
        benchmark_available=True,
        adjustment_semantics="UNKNOWN",
        corporate_action_semantics="UNAVAILABLE",
        survivorship_safe=False,
        ranking_periods=1,
        mature_20_outcomes=0,
        mature_60_outcomes=0,
        status=EvidenceStatus.DIAGNOSTIC_ONLY,
        reasons=("owner watchlist is not survivorship safe",),
    )

    rendered = cli._render_evaluation(
        EvaluationDataset(identity=identity, rows=()),
        EvaluationMetrics(cross_sectional=(), top_k=(), buckets=(), tiers=(), baselines=()),
        readiness,
        export_path=None,
    )

    assert "HISTORICAL_PIT_OWNER_WATCHLIST" in rendered
    assert "PIT owner-watchlist membership remains owner-selected" in rendered
    assert "current-watchlist selection" not in rendered


@pytest.mark.asyncio
async def test_strict_pit_builder_ranks_partial_optional_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict-PIT composition path sends factor-level absence to the shared ranker."""

    def series(identity: InstrumentId, *, sessions: int, daily: Decimal) -> TechnicalSeries:
        prices = [Decimal(100)]
        for _index in range(sessions - 1):
            prices.append(prices[-1] * (Decimal(1) + daily))
        start = CUTOFF.date() - timedelta(days=sessions - 1)
        return TechnicalSeries(
            instrument_id=identity,
            adjustment_status=AdjustmentEvidence.UNKNOWN,
            bars=tuple(
                TechnicalBar(
                    trading_date=start + timedelta(days=index),
                    open=price,
                    high=price * Decimal("1.01"),
                    low=price * Decimal("0.99"),
                    close=price,
                    volume=100_000,
                )
                for index, price in enumerate(prices)
            ),
        )

    partial_id = InstrumentId.deterministic("reference", "partial-strict-pit")
    benchmark_id = InstrumentId.deterministic("reference", "nse-index-nifty-50")
    stored = {
        benchmark_id: series(benchmark_id, sessions=220, daily=Decimal("0.0004")),
        partial_id: series(partial_id, sessions=150, daily=Decimal("0.0015")),
    }

    class FakeWatchlist:
        def __init__(self, _factory: object) -> None:
            pass

        async def execute(self, **_kwargs: object) -> tuple[SimpleNamespace]:
            return (
                SimpleNamespace(
                    identity=SimpleNamespace(
                        instrument_id=partial_id,
                        canonical_symbol="PARTIAL",
                        company_name="Partial Limited",
                    )
                ),
            )

    class FakeHistory:
        def __init__(self, _factory: object) -> None:
            pass

        async def execute(self, query: object) -> TechnicalSeries:
            instrument_id = cast(InstrumentId, cast(SimpleNamespace, query).instrument_id)
            return stored[instrument_id]

    class FakeObservations:
        def __init__(self, _factory: object) -> None:
            pass

        async def execute(self, _query: object) -> tuple[()]:
            return ()

    monkeypatch.setattr(cli, "GetSharedWatchlist", FakeWatchlist)
    monkeypatch.setattr(cli, "GetDailyBarSeries", FakeHistory)
    monkeypatch.setattr(cli, "ListResearchObservations", FakeObservations)
    monkeypatch.setattr(cli, "technical_series_from_daily_bars", lambda value: value)

    ranking = await cli._build_candidates(
        account_id=ACCOUNT,
        cutoff=CUTOFF,
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
    )
    result = ranking.entries[0]

    assert result.eligibility is CandidateEligibility.ELIGIBLE
    assert result.score is not None
    assert (
        FeatureName.MA50_VS_MA200.value,
        FeatureStatus.INSUFFICIENT_HISTORY.value,
    ) in result.feature_availability
    assert any("MA50_VS_MA200: INSUFFICIENT_HISTORY" in item for item in result.missing_evidence)


def test_command_imports_no_provider_or_transport_module() -> None:
    """History reads the local database and has no reachable network client."""
    source = Path(inspect.getfile(cli)).read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    for module in imports:
        lowered = module.lower()
        assert "gdelt" not in lowered
        assert "zerodha" not in lowered
        assert "http" not in lowered
