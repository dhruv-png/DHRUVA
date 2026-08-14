"""Experimental technical research-candidate vocabulary, never advice."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "CANDIDATE_RANKER_REVISION",
    "CandidateEligibility",
    "CandidateRanking",
    "CandidateResult",
    "CandidateTier",
    "EvidenceCompleteness",
]

CANDIDATE_RANKER_REVISION: Final = "technical-candidate-v0"


class CandidateEligibility(StrEnum):
    """Whether the baseline has its minimum defensible evidence."""

    ELIGIBLE = "ELIGIBLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    BENCHMARK_UNAVAILABLE = "BENCHMARK_UNAVAILABLE"
    STALE = "STALE"
    UNSUPPORTED_ADJUSTMENT = "UNSUPPORTED_ADJUSTMENT"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class CandidateTier(StrEnum):
    """Fixed score bands within the experimental baseline."""

    STRONG_CANDIDATE = "STRONG_CANDIDATE"
    CANDIDATE = "CANDIDATE"
    NEUTRAL = "NEUTRAL"
    WEAK = "WEAK"


class EvidenceCompleteness(StrEnum):
    """Input completeness, explicitly not probability or investment certainty."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """One stock's experimental technical research priority."""

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    cutoff: datetime
    eligibility: CandidateEligibility
    score: Decimal | None
    rank: int | None
    universe_percentile: Decimal | None
    relative_strength_60_percentile: Decimal | None
    relative_strength_120_percentile: Decimal | None
    tier: CandidateTier | None
    evidence_completeness: EvidenceCompleteness
    supporting_evidence: tuple[str, ...]
    counterevidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    feature_availability: tuple[tuple[str, str], ...]
    attention_score: int | None = None
    attention_band: AttentionBand | None = None
    attention_cutoff: datetime | None = None
    ranker_revision: str = CANDIDATE_RANKER_REVISION

    def __post_init__(self) -> None:
        """Keep ranked and excluded states internally consistent."""
        invariant(self.canonical_symbol.strip() != "", "candidate symbol cannot be blank")
        invariant(self.company_name.strip() != "", "candidate company cannot be blank")
        ranked = self.eligibility is CandidateEligibility.ELIGIBLE
        invariant(ranked == (self.score is not None), "candidate score contradicts eligibility")
        invariant(ranked == (self.rank is not None), "candidate rank contradicts eligibility")
        invariant(ranked == (self.tier is not None), "candidate tier contradicts eligibility")
        invariant(
            ranked == (self.universe_percentile is not None),
            "candidate percentile contradicts eligibility",
        )
        invariant(
            ranked == (self.relative_strength_60_percentile is not None),
            "60-session relative strength contradicts eligibility",
        )
        invariant(
            ranked == (self.relative_strength_120_percentile is not None),
            "120-session relative strength contradicts eligibility",
        )
        if self.score is not None:
            invariant(Decimal(-100) <= self.score <= Decimal(100), "candidate score out of bounds")
        if self.universe_percentile is not None:
            invariant(
                Decimal(0) <= self.universe_percentile <= Decimal(100),
                "candidate percentile out of bounds",
            )
        for percentile in (
            self.relative_strength_60_percentile,
            self.relative_strength_120_percentile,
        ):
            if percentile is not None:
                invariant(
                    Decimal(0) <= percentile <= Decimal(100),
                    "relative-strength percentile out of bounds",
                )
        invariant(
            (self.attention_score is None) == (self.attention_band is None),
            "attention metadata is incomplete",
        )
        invariant(
            (self.attention_score is None) == (self.attention_cutoff is None),
            "attention cutoff metadata is incomplete",
        )
        if self.attention_cutoff is not None:
            invariant(
                self.attention_cutoff <= self.cutoff,
                "candidate includes attention observed after its cutoff",
            )
        invariant(
            len({name for name, _status in self.feature_availability})
            == len(self.feature_availability),
            "candidate feature availability repeats a feature",
        )


@dataclass(frozen=True, slots=True)
class CandidateRanking:
    """Complete owner-watchlist result, including every explicit exclusion."""

    cutoff: datetime
    entries: tuple[CandidateResult, ...]
    benchmark_symbol: str
    benchmark_basis: str
    feature_revision: str
    limitations: tuple[str, ...]
    ranker_revision: str = CANDIDATE_RANKER_REVISION
    experimental_label: str = "EXPERIMENTAL RESEARCH CANDIDATE"
    universe_label: str = "CURRENT OWNER WATCHLIST"

    def __post_init__(self) -> None:
        """Require all eligible ranks first, contiguous and deterministic."""
        eligible = tuple(item for item in self.entries if item.rank is not None)
        invariant(
            tuple(item.rank for item in eligible) == tuple(range(1, len(eligible) + 1)),
            "candidate ranks must be contiguous",
        )
        invariant(
            all(item.cutoff == self.cutoff for item in self.entries),
            "candidate ranking mixes cutoffs",
        )
        identities = tuple(item.instrument_id for item in self.entries)
        symbols = tuple(item.canonical_symbol for item in self.entries)
        invariant(len(set(identities)) == len(identities), "candidate instrument appears twice")
        invariant(len(set(symbols)) == len(symbols), "candidate symbol appears twice")

    @property
    def eligible_count(self) -> int:
        """Return the number receiving a normal candidate rank."""
        return sum(item.eligibility is CandidateEligibility.ELIGIBLE for item in self.entries)
