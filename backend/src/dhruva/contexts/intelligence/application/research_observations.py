"""Freeze and inspect immutable research-attention observations.

The freeze command accepts an already resolved digest, ranking and market
fingerprints.  It performs no archive read and no provider call, so every
component necessarily describes the caller's one supplied PIT cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.digest import DigestSection
from dhruva.contexts.intelligence.domain.research_observation import (
    AttentionObservationMember,
    ObservationAppendResult,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationType,
    ResearchObservation,
    StoredResearchObservation,
    observation_fingerprint,
    universe_fingerprint,
)
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.attention import ResearchAttention
    from dhruva.contexts.intelligence.domain.digest import WatchlistDigest
    from dhruva.contexts.intelligence.domain.ports import ResearchObservationUnitOfWork
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "FreezeAttentionObservation",
    "FreezeAttentionObservationCommand",
    "ListResearchObservations",
    "ListResearchObservationsQuery",
]

_MAX_HISTORY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class FreezeAttentionObservationCommand:
    """The one PIT-resolved state to preserve, with no implicit clock or read."""

    account_id: AccountId
    cutoff: datetime
    recorded_at: datetime
    digest: WatchlistDigest
    ranked: tuple[ResearchAttention, ...]
    market_context_fingerprints: Mapping[InstrumentId, str]
    packet_body_sha256: str
    provenance: ObservationProvenance
    source_health: ObservationSourceHealth


class FreezeAttentionObservation:
    """Build one deterministic observation and append it idempotently."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], ResearchObservationUnitOfWork],
    ) -> None:
        """Bind the use case to an account-scoped transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, command: FreezeAttentionObservationCommand) -> ObservationAppendResult:
        """Freeze the supplied state without performing another read or clock call."""
        observation = _observation(command)
        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            result = await unit_of_work.observations.append(observation)
            await unit_of_work.commit()
        return result


@dataclass(frozen=True, slots=True)
class ListResearchObservationsQuery:
    """An account-scoped bounded history query."""

    account_id: AccountId
    limit: int = 20


class ListResearchObservations:
    """Read recent accumulated observations without consulting a provider."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], ResearchObservationUnitOfWork],
    ) -> None:
        """Bind the query to an account-scoped transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self, query: ListResearchObservationsQuery
    ) -> tuple[StoredResearchObservation, ...]:
        """Return newest observations after validating the explicit bound."""
        if not 1 <= query.limit <= _MAX_HISTORY_LIMIT:
            raise ValidationError(
                "research history limit is outside its permitted bounds",
                limit=query.limit,
                maximum=_MAX_HISTORY_LIMIT,
            )
        async with self._unit_of_work_factory(query.account_id) as unit_of_work:
            return await unit_of_work.observations.list_recent(limit=query.limit)


def _observation(command: FreezeAttentionObservationCommand) -> ResearchObservation:
    """Assemble the immutable domain fact from one resolved research state."""
    if command.digest.known_at != command.cutoff:
        raise ValidationError(
            "digest cutoff does not match observation cutoff",
            digest_cutoff=command.digest.known_at.isoformat(),
            observation_cutoff=command.cutoff.isoformat(),
        )
    if any(entry.as_of != command.cutoff for entry in command.ranked):
        raise ValidationError("attention ranking contains a different PIT cutoff")

    sections = {section.instrument_id: section for section in command.digest.sections}
    ranked_ids = tuple(entry.instrument_id for entry in command.ranked)
    if set(ranked_ids) != set(sections):
        raise ValidationError("attention ranking and digest universe do not match")
    if set(command.market_context_fingerprints) != set(ranked_ids):
        raise ValidationError("market-context fingerprints do not cover the ranked universe")

    members = tuple(
        _member(
            rank,
            entry,
            section=sections[entry.instrument_id],
            market_context_sha256=command.market_context_fingerprints[entry.instrument_id],
        )
        for rank, entry in enumerate(command.ranked, start=1)
    )
    universe_sha256 = universe_fingerprint(members)
    fingerprint = observation_fingerprint(
        account_id=command.account_id,
        observation_type=ObservationType.ATTENTION_OBSERVATION,
        cutoff=command.cutoff,
        universe_sha256=universe_sha256,
        packet_body_sha256=command.packet_body_sha256,
        provenance=command.provenance,
        source_health=command.source_health,
        members=members,
    )
    return ResearchObservation(
        account_id=command.account_id,
        observation_type=ObservationType.ATTENTION_OBSERVATION,
        cutoff=command.cutoff,
        recorded_at=command.recorded_at,
        provenance=command.provenance,
        source_health=command.source_health,
        members=members,
        universe_sha256=universe_sha256,
        observation_sha256=fingerprint,
        packet_body_sha256=command.packet_body_sha256,
    )


def _member(
    rank: int,
    entry: ResearchAttention,
    *,
    section: DigestSection,
    market_context_sha256: str,
) -> AttentionObservationMember:
    """Project one ranking and its already-resolved digest evidence."""
    revisions = tuple(item.item.revision.revision for item in section.entries)
    return AttentionObservationMember(
        instrument_id=entry.instrument_id,
        rank=rank,
        canonical_symbol=entry.canonical_symbol,
        company_name=entry.company_name,
        score=entry.score,
        band=entry.band,
        reasons=entry.reasons,
        market_context_available=entry.market_context_available,
        market_context_sha256=market_context_sha256,
        archived_news_revisions=revisions,
        news_items_withheld=section.withheld,
    )
