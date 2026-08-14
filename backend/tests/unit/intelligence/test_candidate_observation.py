"""Candidate freezes hash every durable fact and ignore retry wall-clock time."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from dhruva.contexts.intelligence.domain.candidate_observation import (
    candidate_observation_fingerprint,
    candidate_observation_payload,
    candidate_universe_fingerprint,
)
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateRanking,
    CandidateResult,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.shared.identity import AccountId, InstrumentId

CUTOFF = datetime(2026, 8, 14, 10, tzinfo=UTC)
ACCOUNT = AccountId.deterministic("owner-family")


def _ranking(*, score: Decimal = Decimal("25.00")) -> CandidateRanking:
    return CandidateRanking(
        cutoff=CUTOFF,
        entries=(
            CandidateResult(
                instrument_id=InstrumentId.deterministic("reference", "hal"),
                canonical_symbol="HAL",
                company_name="Hindustan Aeronautics Limited",
                cutoff=CUTOFF,
                eligibility=CandidateEligibility.ELIGIBLE,
                score=score,
                rank=1,
                universe_percentile=Decimal("100.0"),
                relative_strength_60_percentile=Decimal("100.0"),
                relative_strength_120_percentile=Decimal("100.0"),
                tier=CandidateTier.CANDIDATE,
                evidence_completeness=EvidenceCompleteness.LOW,
                supporting_evidence=("relative momentum is above the universe median",),
                counterevidence=(),
                missing_evidence=("corporate-action adjustment status is UNKNOWN",),
                feature_availability=(("RETURN_120", "AVAILABLE"),),
            ),
        ),
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        feature_revision="technical-features-v0",
        limitations=("benchmark is a price index; dividends are excluded",),
    )


def test_same_state_has_the_same_candidate_fingerprint() -> None:
    """Account, state, cutoff, and revisions fully determine the fingerprint."""
    ranking = _ranking()
    universe = candidate_universe_fingerprint(ranking)

    first = candidate_observation_fingerprint(
        account_id=ACCOUNT, ranking=ranking, universe_sha256=universe
    )
    retry = candidate_observation_fingerprint(
        account_id=ACCOUNT, ranking=ranking, universe_sha256=universe
    )

    assert first == retry
    assert len(first) == 64


def test_score_and_feature_availability_participate_in_the_payload_hash() -> None:
    """A changed result cannot collide with the original official freeze."""
    original = _ranking()
    changed = _ranking(score=Decimal("26.00"))

    original_hash = candidate_observation_fingerprint(
        account_id=ACCOUNT,
        ranking=original,
        universe_sha256=candidate_universe_fingerprint(original),
    )
    changed_hash = candidate_observation_fingerprint(
        account_id=ACCOUNT,
        ranking=changed,
        universe_sha256=candidate_universe_fingerprint(changed),
    )

    assert original_hash != changed_hash
    payload = candidate_observation_payload(original)
    entries = payload["entries"]
    assert isinstance(entries, list)
    assert entries[0]["feature_availability"] == {"RETURN_120": "AVAILABLE"}
