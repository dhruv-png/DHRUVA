"""Immutable facts recording what DHRUVA's research attention showed.

An observation is evidence about a past product state, not advice about a
future price.  It records the existing deterministic attention ranking at one
point-in-time cutoff and enough provenance to reproduce and inspect it later.

The fingerprint deliberately excludes ``recorded_at``.  A retry can happen
later, but if account, cutoff, rulesets, source health and every ranked member
are unchanged it is the same logical observation and must have the same
identity.  A genuinely changed input at the same cutoff produces a different
fingerprint and is appended as a correction by persistence; it never rewrites
the earlier fact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.contexts.intelligence.domain.attention import (
    MAX_ATTENTION_SCORE,
    AttentionBand,
    band_for_score,
)
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "OBSERVATION_SCHEMA_REVISION",
    "AttentionObservationMember",
    "ObservationAppendResult",
    "ObservationProvenance",
    "ObservationRunStatus",
    "ObservationSourceHealth",
    "ObservationSourceStatus",
    "ObservationType",
    "ResearchObservation",
    "StoredResearchObservation",
    "observation_fingerprint",
    "universe_fingerprint",
]

OBSERVATION_SCHEMA_REVISION: Final = "research-observation-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[a-z][a-z0-9._-]{1,127}\Z")


class ObservationType(StrEnum):
    """Kinds of frozen research state, extensible without renaming the ledger."""

    ATTENTION_OBSERVATION = "ATTENTION_OBSERVATION"


class ObservationRunStatus(StrEnum):
    """Whether the frozen state followed healthy or degraded source phases."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


class ObservationSourceStatus(StrEnum):
    """One input phase's terminal status at the time the observation froze."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class ObservationProvenance:
    """Ruleset and schema identities used to assemble one observation."""

    attention_revision: str
    digest_revision: str
    digest_policy_revision: str
    entity_linking_revision: str
    event_classification_revision: str
    market_context_revision: str
    news_identity_revision: str
    sentiment_revision: str
    packet_schema_revision: str
    observation_schema_revision: str = OBSERVATION_SCHEMA_REVISION

    def __post_init__(self) -> None:
        """Require explicit, machine-checkable revision identities."""
        for name, value in self.as_pairs():
            invariant(bool(_REVISION.fullmatch(value)), f"invalid {name}", revision=value)

    def as_pairs(self) -> tuple[tuple[str, str], ...]:
        """Return a fixed-order representation for canonical hashing."""
        return (
            ("attention", self.attention_revision),
            ("digest", self.digest_revision),
            ("digest_policy", self.digest_policy_revision),
            ("entity_linking", self.entity_linking_revision),
            ("event_classification", self.event_classification_revision),
            ("market_context", self.market_context_revision),
            ("news_identity", self.news_identity_revision),
            ("sentiment", self.sentiment_revision),
            ("packet_schema", self.packet_schema_revision),
            ("observation_schema", self.observation_schema_revision),
        )


@dataclass(frozen=True, slots=True)
class ObservationSourceHealth:
    """The source-phase truth preserved beside a frozen research state."""

    market: ObservationSourceStatus
    news: ObservationSourceStatus
    degraded_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require reasons exactly when the overall run is not healthy."""
        invariant(
            all(reason.strip() for reason in self.degraded_reasons),
            "a degraded reason cannot be blank",
        )
        invariant(
            len(set(self.degraded_reasons)) == len(self.degraded_reasons),
            "a degraded reason cannot be repeated",
        )
        if self.status is ObservationRunStatus.HEALTHY:
            invariant(not self.degraded_reasons, "a healthy observation cannot carry degradation")
        else:
            invariant(bool(self.degraded_reasons), "a degraded observation must say why")

    @property
    def status(self) -> ObservationRunStatus:
        """Reduce source phases to the observation's honest overall status."""
        if self.market is self.news is ObservationSourceStatus.HEALTHY:
            return ObservationRunStatus.HEALTHY
        return ObservationRunStatus.DEGRADED


@dataclass(frozen=True, slots=True)
class AttentionObservationMember:
    """One watchlist member in the exact deterministic attention order."""

    instrument_id: InstrumentId
    rank: int
    canonical_symbol: str
    company_name: str
    score: int
    band: AttentionBand
    reasons: tuple[str, ...]
    market_context_available: bool
    market_context_sha256: str
    archived_news_revisions: tuple[str, ...]
    news_items_withheld: int = 0

    def __post_init__(self) -> None:
        """Refuse a member whose persisted verdict contradicts itself."""
        invariant(self.rank >= 1, "an observation rank must be positive")
        invariant(self.canonical_symbol.strip() != "", "an observation symbol cannot be blank")
        invariant(self.company_name.strip() != "", "an observation company name cannot be blank")
        invariant(0 <= self.score <= MAX_ATTENTION_SCORE, "attention score is out of bounds")
        invariant(self.band is band_for_score(self.score), "attention band contradicts score")
        invariant(all(reason.strip() for reason in self.reasons), "an attention reason is blank")
        invariant(
            bool(_SHA256.fullmatch(self.market_context_sha256)),
            "invalid market-context fingerprint",
        )
        invariant(
            all(_SHA256.fullmatch(revision) for revision in self.archived_news_revisions),
            "invalid archived-news revision",
        )
        invariant(
            len(set(self.archived_news_revisions)) == len(self.archived_news_revisions),
            "an archived-news revision cannot be repeated for one member",
        )
        invariant(self.news_items_withheld >= 0, "a withheld-news count cannot be negative")

    def canonical(self) -> dict[str, object]:
        """Return the stable shape that participates in the observation hash."""
        return {
            "instrument_id": str(self.instrument_id),
            "rank": self.rank,
            "canonical_symbol": self.canonical_symbol,
            "company_name": self.company_name,
            "score": self.score,
            "band": self.band.value,
            "reasons": list(self.reasons),
            "market_context_available": self.market_context_available,
            "market_context_sha256": self.market_context_sha256,
            "archived_news_revisions": list(self.archived_news_revisions),
            "news_items_withheld": self.news_items_withheld,
        }


def universe_fingerprint(members: tuple[AttentionObservationMember, ...]) -> str:
    """Hash watchlist identity independently of its current attention order."""
    universe = sorted(
        (
            {
                "instrument_id": str(member.instrument_id),
                "canonical_symbol": member.canonical_symbol,
                "company_name": member.company_name,
            }
            for member in members
        ),
        key=lambda item: str(item["instrument_id"]),
    )
    return _fingerprint(universe)


def observation_fingerprint(  # noqa: PLR0913 - every field is an identity axis
    *,
    account_id: AccountId,
    observation_type: ObservationType,
    cutoff: datetime,
    universe_sha256: str,
    packet_body_sha256: str,
    provenance: ObservationProvenance,
    source_health: ObservationSourceHealth,
    members: tuple[AttentionObservationMember, ...],
) -> str:
    """Hash every immutable input that makes one logical observation distinct."""
    body = {
        "account_id": str(account_id),
        "observation_type": observation_type.value,
        "cutoff": cutoff.isoformat(),
        "universe_sha256": universe_sha256,
        "packet_body_sha256": packet_body_sha256,
        "provenance": dict(provenance.as_pairs()),
        "source_health": {
            "market": source_health.market.value,
            "news": source_health.news.value,
            "status": source_health.status.value,
            "degraded_reasons": list(source_health.degraded_reasons),
        },
        "members": [member.canonical() for member in members],
    }
    return _fingerprint(body)


@dataclass(frozen=True, slots=True)
class ResearchObservation:
    """One complete, immutable research-attention fact at a PIT cutoff."""

    account_id: AccountId
    observation_type: ObservationType
    cutoff: datetime
    recorded_at: datetime
    provenance: ObservationProvenance
    source_health: ObservationSourceHealth
    members: tuple[AttentionObservationMember, ...]
    universe_sha256: str
    observation_sha256: str
    packet_body_sha256: str

    def __post_init__(self) -> None:
        """Require chronology, complete ranks and self-verifying fingerprints."""
        _utc(self.cutoff, field="cutoff")
        _utc(self.recorded_at, field="recorded_at")
        invariant(
            self.recorded_at >= self.cutoff,
            "an observation cannot be recorded before cutoff",
        )
        invariant(
            tuple(member.rank for member in self.members) == tuple(range(1, len(self.members) + 1)),
            "observation ranks must be contiguous and ordered",
        )
        identities = tuple(member.instrument_id for member in self.members)
        invariant(len(set(identities)) == len(identities), "an instrument cannot be ranked twice")
        invariant(bool(_SHA256.fullmatch(self.universe_sha256)), "invalid universe fingerprint")
        invariant(
            bool(_SHA256.fullmatch(self.packet_body_sha256)),
            "invalid packet-body fingerprint",
        )
        invariant(
            self.universe_sha256 == universe_fingerprint(self.members),
            "universe fingerprint does not match members",
        )
        expected = observation_fingerprint(
            account_id=self.account_id,
            observation_type=self.observation_type,
            cutoff=self.cutoff,
            universe_sha256=self.universe_sha256,
            packet_body_sha256=self.packet_body_sha256,
            provenance=self.provenance,
            source_health=self.source_health,
            members=self.members,
        )
        invariant(self.observation_sha256 == expected, "observation fingerprint does not match")

    @property
    def status(self) -> ObservationRunStatus:
        """Expose the status derived from the preserved source health."""
        return self.source_health.status

    @property
    def attention_count(self) -> int:
        """Count members carrying at least one attention point."""
        return sum(member.score > 0 for member in self.members)


@dataclass(frozen=True, slots=True)
class StoredResearchObservation:
    """A frozen observation plus the earlier fact it explicitly corrects."""

    observation: ResearchObservation
    supersedes_sha256: str | None = None

    def __post_init__(self) -> None:
        """Require any supersession link to name a valid, different fingerprint."""
        if self.supersedes_sha256 is not None:
            invariant(bool(_SHA256.fullmatch(self.supersedes_sha256)), "invalid supersession hash")
            invariant(
                self.supersedes_sha256 != self.observation.observation_sha256,
                "an observation cannot supersede itself",
            )


@dataclass(frozen=True, slots=True)
class ObservationAppendResult:
    """Whether persistence appended a new fact or found the identical one."""

    stored: StoredResearchObservation
    created: bool


def _fingerprint(value: object) -> str:
    """Return SHA-256 over canonical UTF-8 JSON."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime, *, field: str) -> None:
    """Require a timezone-aware UTC instant (ADR-006)."""
    invariant(value.tzinfo is not None and value.utcoffset() is not None, f"{field} must be aware")
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")
