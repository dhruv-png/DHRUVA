"""Append-only freeze identity for one experimental candidate ranking."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.candidates import CandidateRanking
    from dhruva.shared.identity import AccountId

__all__ = [
    "CANDIDATE_OBSERVATION_SCHEMA_REVISION",
    "CandidateObservation",
    "CandidateObservationAppendResult",
    "candidate_observation_fingerprint",
    "candidate_observation_payload",
    "candidate_universe_fingerprint",
]

CANDIDATE_OBSERVATION_SCHEMA_REVISION: Final = "candidate-observation-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def candidate_universe_fingerprint(ranking: CandidateRanking) -> str:
    """Hash identities independently of rank, eligibility, or score."""
    universe = sorted(
        (
            {
                "instrument_id": str(item.instrument_id),
                "canonical_symbol": item.canonical_symbol,
                "company_name": item.company_name,
            }
            for item in ranking.entries
        ),
        key=lambda item: str(item["instrument_id"]),
    )
    return _fingerprint(universe)


def candidate_observation_payload(ranking: CandidateRanking) -> dict[str, object]:
    """Return the complete stable JSON shape protected by the observation hash."""
    return {
        "schema_revision": CANDIDATE_OBSERVATION_SCHEMA_REVISION,
        "cutoff": ranking.cutoff.isoformat(),
        "experimental_label": ranking.experimental_label,
        "universe_label": ranking.universe_label,
        "benchmark_symbol": ranking.benchmark_symbol,
        "benchmark_basis": ranking.benchmark_basis,
        "feature_revision": ranking.feature_revision,
        "ranker_revision": ranking.ranker_revision,
        "limitations": list(ranking.limitations),
        "entries": [
            {
                "instrument_id": str(item.instrument_id),
                "canonical_symbol": item.canonical_symbol,
                "company_name": item.company_name,
                "eligibility": item.eligibility.value,
                "score": None if item.score is None else format(item.score, "f"),
                "rank": item.rank,
                "universe_percentile": (
                    None
                    if item.universe_percentile is None
                    else format(item.universe_percentile, "f")
                ),
                "relative_strength_60_percentile": (
                    None
                    if item.relative_strength_60_percentile is None
                    else format(item.relative_strength_60_percentile, "f")
                ),
                "relative_strength_120_percentile": (
                    None
                    if item.relative_strength_120_percentile is None
                    else format(item.relative_strength_120_percentile, "f")
                ),
                "tier": None if item.tier is None else item.tier.value,
                "evidence_completeness": item.evidence_completeness.value,
                "supporting_evidence": list(item.supporting_evidence),
                "counterevidence": list(item.counterevidence),
                "missing_evidence": list(item.missing_evidence),
                "feature_availability": dict(item.feature_availability),
                "attention_score": item.attention_score,
                "attention_band": (
                    None if item.attention_band is None else item.attention_band.value
                ),
                "attention_cutoff": (
                    None if item.attention_cutoff is None else item.attention_cutoff.isoformat()
                ),
            }
            for item in ranking.entries
        ],
    }


def candidate_observation_fingerprint(
    *, account_id: AccountId, ranking: CandidateRanking, universe_sha256: str
) -> str:
    """Hash account, universe, revisions, cutoff, and every frozen candidate fact."""
    return _fingerprint(
        {
            "account_id": str(account_id),
            "observation_type": "CANDIDATE_RANKING",
            "universe_sha256": universe_sha256,
            "payload": candidate_observation_payload(ranking),
        }
    )


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """One complete official freeze of the experimental technical baseline."""

    account_id: AccountId
    ranking: CandidateRanking
    recorded_at: datetime
    universe_sha256: str
    observation_sha256: str

    def __post_init__(self) -> None:
        """Require UTC chronology and self-verifying immutable fingerprints."""
        _utc(self.ranking.cutoff, field="cutoff")
        _utc(self.recorded_at, field="recorded_at")
        invariant(self.recorded_at >= self.ranking.cutoff, "freeze cannot precede its cutoff")
        invariant(bool(_SHA256.fullmatch(self.universe_sha256)), "invalid universe fingerprint")
        invariant(
            self.universe_sha256 == candidate_universe_fingerprint(self.ranking),
            "candidate universe fingerprint does not match",
        )
        expected = candidate_observation_fingerprint(
            account_id=self.account_id,
            ranking=self.ranking,
            universe_sha256=self.universe_sha256,
        )
        invariant(self.observation_sha256 == expected, "candidate observation hash does not match")


@dataclass(frozen=True, slots=True)
class CandidateObservationAppendResult:
    """Whether a candidate freeze was inserted or was an identical retry."""

    observation: CandidateObservation
    created: bool


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime, *, field: str) -> None:
    invariant(value.tzinfo is not None and value.utcoffset() is not None, f"{field} must be aware")
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")
