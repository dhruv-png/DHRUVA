"""Point-in-time evaluation universes and survivorship evidence.

Historical membership is source evidence, not a reconstruction from today's
symbols.  Effective time answers *who belonged then*; ``known_at`` answers
*when the source fact was observable*.  Both are required for strict PIT use.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "HistoricalDataQualityReason",
    "HistoricalUniverseDefinition",
    "HistoricalUniverseMember",
    "HistoricalUniverseMembershipRevision",
    "MembershipReason",
    "ResolvedHistoricalUniverse",
    "SourceDiligenceStatus",
    "SurvivorshipAssessment",
    "SurvivorshipStatus",
    "UniverseKind",
    "assess_survivorship",
    "historical_universe_fingerprint",
]

_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{1,127}\Z")
_SOURCE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}\Z")


class UniverseKind(StrEnum):
    """Provider-neutral intent of a membership set."""

    CURRENT_OWNER_WATCHLIST = "CURRENT_OWNER_WATCHLIST"
    HISTORICAL_PIT_OWNER_WATCHLIST = "HISTORICAL_PIT_OWNER_WATCHLIST"
    HISTORICAL_MARKET_UNIVERSE = "HISTORICAL_MARKET_UNIVERSE"
    INDEX_CONSTITUENT_UNIVERSE = "INDEX_CONSTITUENT_UNIVERSE"
    LIQUIDITY_FILTERED_UNIVERSE = "LIQUIDITY_FILTERED_UNIVERSE"


class MembershipReason(StrEnum):
    """Why a security is included for an effective interval."""

    OWNER_SELECTION = "OWNER_SELECTION"
    INDEX_CONSTITUENT = "INDEX_CONSTITUENT"
    MARKET_ELIGIBLE = "MARKET_ELIGIBLE"
    LIQUIDITY_ELIGIBLE = "LIQUIDITY_ELIGIBLE"
    SOURCE_REPORTED = "SOURCE_REPORTED"


class SourceDiligenceStatus(StrEnum):
    """Review state; never an implied licence or source endorsement."""

    UNREVIEWED = "UNREVIEWED"
    DOCUMENTATION_REVIEWED = "DOCUMENTATION_REVIEWED"
    PIT_UNPROVEN = "PIT_UNPROVEN"
    LICENSING_UNCLEAR = "LICENSING_UNCLEAR"
    TECHNICALLY_SUITABLE = "TECHNICALLY_SUITABLE"
    OWNER_APPROVAL_REQUIRED = "OWNER_APPROVAL_REQUIRED"
    REJECTED = "REJECTED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class SurvivorshipStatus(StrEnum):
    """Monotone evidence levels for an evaluation universe."""

    CURRENT_SELECTION_ONLY = "CURRENT_SELECTION_ONLY"
    HISTORICAL_MEMBERSHIP_AVAILABLE = "HISTORICAL_MEMBERSHIP_AVAILABLE"
    REMOVALS_INCLUDED = "REMOVALS_INCLUDED"
    DELISTINGS_INCLUDED = "DELISTINGS_INCLUDED"
    PIT_KNOWN_AT_AVAILABLE = "PIT_KNOWN_AT_AVAILABLE"
    SURVIVORSHIP_SAFE = "SURVIVORSHIP_SAFE"
    UNKNOWN = "UNKNOWN"


class HistoricalDataQualityReason(StrEnum):
    """Stable failure/degradation reasons carried into readiness evidence."""

    HISTORICAL_UNIVERSE_UNAVAILABLE = "HISTORICAL_UNIVERSE_UNAVAILABLE"
    UNIVERSE_MEMBERSHIP_UNKNOWN = "UNIVERSE_MEMBERSHIP_UNKNOWN"
    DELISTING_COVERAGE_UNKNOWN = "DELISTING_COVERAGE_UNKNOWN"
    CORPORATE_ACTION_UNVERIFIED = "CORPORATE_ACTION_UNVERIFIED"
    ADJUSTMENT_UNKNOWN = "ADJUSTMENT_UNKNOWN"
    BENCHMARK_TRI_UNAVAILABLE = "BENCHMARK_TRI_UNAVAILABLE"
    PIT_KNOWN_AT_UNAVAILABLE = "PIT_KNOWN_AT_UNAVAILABLE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    SOURCE_LICENSING_UNRESOLVED = "SOURCE_LICENSING_UNRESOLVED"
    INSTRUMENT_MAPPING_UNRESOLVED = "INSTRUMENT_MAPPING_UNRESOLVED"


def _utc(value: datetime) -> None:
    invariant(value.tzinfo is not None and value.utcoffset() is not None, "known_at needs zone")
    invariant(value.utcoffset() == timedelta(0), "known_at must be UTC")


@dataclass(frozen=True, slots=True)
class HistoricalUniverseDefinition:
    """One immutable source revision describing a logical universe."""

    account_id: AccountId
    universe_id: str
    label: str
    kind: UniverseKind
    known_at: datetime
    source: str
    source_revision: str
    source_status: SourceDiligenceStatus
    historical_membership_available: bool
    removals_included: bool
    delistings_included: bool
    pit_known_at_available: bool
    instrument_lifecycle_available: bool
    licensing_confirmed: bool

    def __post_init__(self) -> None:
        """Reject anonymous or internally contradictory source claims."""
        invariant(bool(_KEY.fullmatch(self.universe_id)), "invalid historical universe id")
        invariant(self.label.strip() == self.label and bool(self.label), "universe label is blank")
        invariant(bool(_SOURCE.fullmatch(self.source)), "invalid universe source")
        invariant(bool(_KEY.fullmatch(self.source_revision)), "invalid universe source revision")
        _utc(self.known_at)
        if self.kind is UniverseKind.CURRENT_OWNER_WATCHLIST:
            invariant(not self.removals_included, "current selection cannot claim removals")
            invariant(not self.delistings_included, "current selection cannot claim delistings")
        if self.pit_known_at_available:
            invariant(
                self.historical_membership_available,
                "PIT known-at requires historical membership",
            )


@dataclass(frozen=True, slots=True)
class HistoricalUniverseMembershipRevision:
    """Append-only effective membership fact from one source revision."""

    account_id: AccountId
    universe_id: str
    instrument_id: InstrumentId
    effective_from: date
    effective_to: date | None
    known_at: datetime
    source: str
    source_revision: str
    reason: MembershipReason
    source_member_key: str
    is_delisted: bool = False

    def __post_init__(self) -> None:
        """Require bounded bitemporal and provenance semantics."""
        invariant(bool(_KEY.fullmatch(self.universe_id)), "invalid historical universe id")
        invariant(
            self.effective_to is None or self.effective_to >= self.effective_from,
            "membership effective interval is reversed",
        )
        _utc(self.known_at)
        invariant(bool(_SOURCE.fullmatch(self.source)), "invalid membership source")
        invariant(bool(_KEY.fullmatch(self.source_revision)), "invalid membership revision")
        invariant(self.source_member_key.strip() != "", "source member key is blank")

    def active_on(self, cutoff: date) -> bool:
        """Return whether the member belongs at the effective cutoff."""
        return self.effective_from <= cutoff and (
            self.effective_to is None or cutoff <= self.effective_to
        )


@dataclass(frozen=True, slots=True)
class HistoricalUniverseMember:
    """Resolved membership joined to the PIT-effective instrument identity."""

    membership: HistoricalUniverseMembershipRevision
    canonical_symbol: str
    company_name: str
    identity_source_revision: str


@dataclass(frozen=True, slots=True)
class SurvivorshipAssessment:
    """Fail-closed classification plus exact missing evidence."""

    status: SurvivorshipStatus
    survivorship_safe: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedHistoricalUniverse:
    """Membership actually observable for one effective and knowledge cutoff."""

    definition: HistoricalUniverseDefinition
    effective_on: date
    known_at: datetime
    members: tuple[HistoricalUniverseMember, ...]
    survivorship: SurvivorshipAssessment
    fingerprint: str

    def __post_init__(self) -> None:
        """Keep output deterministic and self-verifying."""
        symbols = tuple(item.canonical_symbol for item in self.members)
        invariant(symbols == tuple(sorted(symbols)), "historical members are not sorted")
        invariant(
            self.fingerprint
            == historical_universe_fingerprint(
                self.definition, self.effective_on, self.known_at, self.members
            ),
            "historical universe fingerprint mismatch",
        )


def assess_survivorship(definition: HistoricalUniverseDefinition) -> SurvivorshipAssessment:
    """Classify only the strongest evidence level whose prerequisites all hold."""
    if definition.kind is UniverseKind.CURRENT_OWNER_WATCHLIST:
        return SurvivorshipAssessment(
            status=SurvivorshipStatus.CURRENT_SELECTION_ONLY,
            survivorship_safe=False,
            blockers=(
                "historical universe membership is unavailable",
                "removal coverage is unavailable",
                "delisting coverage is unavailable",
                "PIT known-at membership is unavailable",
            ),
        )
    blockers: list[str] = []
    for available, reason in (
        (definition.historical_membership_available, "historical membership is unavailable"),
        (definition.removals_included, "removal coverage is unavailable"),
        (definition.delistings_included, "delisting coverage is unavailable"),
        (definition.pit_known_at_available, "PIT known-at membership is unavailable"),
        (definition.instrument_lifecycle_available, "instrument lifecycle is unavailable"),
        (definition.licensing_confirmed, "source licensing/retention rights are unresolved"),
    ):
        if not available:
            blockers.append(reason)
    if not blockers:
        return SurvivorshipAssessment(SurvivorshipStatus.SURVIVORSHIP_SAFE, True, ())
    if not definition.historical_membership_available:
        status = SurvivorshipStatus.UNKNOWN
    elif not definition.removals_included:
        status = SurvivorshipStatus.HISTORICAL_MEMBERSHIP_AVAILABLE
    elif not definition.delistings_included:
        status = SurvivorshipStatus.REMOVALS_INCLUDED
    elif not definition.pit_known_at_available:
        status = SurvivorshipStatus.DELISTINGS_INCLUDED
    else:
        status = SurvivorshipStatus.PIT_KNOWN_AT_AVAILABLE
    return SurvivorshipAssessment(status, False, tuple(blockers))


def historical_universe_fingerprint(
    definition: HistoricalUniverseDefinition,
    effective_on: date,
    known_at: datetime,
    members: tuple[HistoricalUniverseMember, ...],
) -> str:
    """Hash the exact definition, cutoffs, memberships, and mapping revisions."""
    payload = {
        "universe_id": definition.universe_id,
        "definition_revision": definition.source_revision,
        "effective_on": effective_on.isoformat(),
        "known_at": known_at.isoformat(),
        "members": [
            {
                "instrument_id": str(item.membership.instrument_id),
                "effective_from": item.membership.effective_from.isoformat(),
                "effective_to": (
                    None
                    if item.membership.effective_to is None
                    else item.membership.effective_to.isoformat()
                ),
                "known_at": item.membership.known_at.isoformat(),
                "source_revision": item.membership.source_revision,
                "identity_source_revision": item.identity_source_revision,
            }
            for item in members
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
