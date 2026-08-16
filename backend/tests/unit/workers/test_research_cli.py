"""The research-history CLI is compact, local-only and semantically explicit."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.contexts.intelligence.domain.candidates import CandidateRanking
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
    assert outcomes.cost_bps == Decimal(20)
    assert evidence.command == "evidence"


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
