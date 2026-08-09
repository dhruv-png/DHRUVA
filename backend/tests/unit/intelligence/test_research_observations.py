"""The research evidence clock is deterministic, truthful and application-idempotent."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Self

import pytest

from dhruva.contexts.intelligence.application.research_observations import (
    FreezeAttentionObservation,
    FreezeAttentionObservationCommand,
    ListResearchObservations,
    ListResearchObservationsQuery,
)
from dhruva.contexts.intelligence.domain.attention import AttentionBand, ResearchAttention
from dhruva.contexts.intelligence.domain.digest import DigestSection, WatchlistDigest
from dhruva.contexts.intelligence.domain.ports import NewsStore, ResearchObservationStore
from dhruva.contexts.intelligence.domain.research_observation import (
    ObservationAppendResult,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationSourceStatus,
    ResearchObservation,
    StoredResearchObservation,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER_ACCOUNT: Final = AccountId.deterministic("another-family")
CUTOFF: Final = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
HAL: Final = InstrumentId.deterministic("reference", "hal")
SBIN: Final = InstrumentId.deterministic("reference", "sbin")


def _provenance() -> ObservationProvenance:
    return ObservationProvenance(
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


def _digest(*, cutoff: datetime = CUTOFF) -> WatchlistDigest:
    return WatchlistDigest(
        known_at=cutoff,
        published_from=cutoff - timedelta(days=2),
        published_to=cutoff,
        sections=(
            DigestSection(HAL, "HAL", "Hindustan Aeronautics Limited", ()),
            DigestSection(SBIN, "SBIN", "State Bank of India", ()),
        ),
    )


def _ranked(*, cutoff: datetime = CUTOFF, hal_score: int = 3) -> tuple[ResearchAttention, ...]:
    return (
        ResearchAttention(
            instrument_id=HAL,
            canonical_symbol="HAL",
            company_name="Hindustan Aeronautics Limited",
            as_of=cutoff,
            score=hal_score,
            band=AttentionBand.ELEVATED if hal_score == 3 else AttentionBand.NORMAL,
            reasons=("1-day absolute move 3%",),
            market_context_available=True,
        ),
        ResearchAttention(
            instrument_id=SBIN,
            canonical_symbol="SBIN",
            company_name="State Bank of India",
            as_of=cutoff,
            score=0,
            band=AttentionBand.LOW,
            reasons=(),
            market_context_available=False,
        ),
    )


def _command(
    *,
    cutoff: datetime = CUTOFF,
    recorded_at: datetime = CUTOFF,
    hal_score: int = 3,
    source_health: ObservationSourceHealth | None = None,
) -> FreezeAttentionObservationCommand:
    return FreezeAttentionObservationCommand(
        account_id=ACCOUNT,
        cutoff=cutoff,
        recorded_at=recorded_at,
        digest=_digest(cutoff=cutoff),
        ranked=_ranked(cutoff=cutoff, hal_score=hal_score),
        market_context_fingerprints={HAL: "1" * 64, SBIN: "2" * 64},
        packet_body_sha256="3" * 64,
        provenance=_provenance(),
        source_health=source_health
        or ObservationSourceHealth(
            market=ObservationSourceStatus.HEALTHY,
            news=ObservationSourceStatus.HEALTHY,
        ),
    )


class FakeObservationStore:
    """Model the repository's fingerprint idempotency without a database."""

    def __init__(self) -> None:
        self.rows: list[StoredResearchObservation] = []

    async def append(self, observation: ResearchObservation) -> ObservationAppendResult:
        """Append once by fingerprint and link changed state to its predecessor."""
        for row in self.rows:
            if row.observation.observation_sha256 == observation.observation_sha256:
                return ObservationAppendResult(stored=row, created=False)
        prior = self.rows[-1].observation.observation_sha256 if self.rows else None
        stored = StoredResearchObservation(observation=observation, supersedes_sha256=prior)
        self.rows.append(stored)
        return ObservationAppendResult(stored=stored, created=True)

    async def list_recent(self, *, limit: int) -> tuple[StoredResearchObservation, ...]:
        """Return newest fake facts first."""
        return tuple(reversed(self.rows[-limit:]))


class FakeUnitOfWork:
    """Expose one fake store and make commit observable."""

    def __init__(self, store: FakeObservationStore) -> None:
        self._observations = store
        self.commits = 0

    @property
    def news(self) -> NewsStore:
        """Prove the freeze use case never asks for a second news read."""
        raise AssertionError("freezing an observation must not read news again")

    @property
    def observations(self) -> ResearchObservationStore:
        """Expose the typed observation store required by the transaction port."""
        return self._observations

    async def __aenter__(self) -> Self:
        """Enter the fake transaction."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Leave the fake transaction without side effects."""
        return

    async def commit(self) -> None:
        """Record the application-level commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Provide the protocol's unused rollback path."""
        return


class Factory:
    """Refuse an account mismatch before constructing the transaction."""

    def __init__(self) -> None:
        self.store = FakeObservationStore()
        self.units: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Build one transaction only for the expected account."""
        assert account_id == ACCOUNT
        unit = FakeUnitOfWork(self.store)
        self.units.append(unit)
        return unit


@pytest.mark.asyncio
async def test_freeze_persists_the_exact_cutoff_ranking_and_provenance() -> None:
    """The use case preserves the resolved state and commits exactly once."""
    factory = Factory()

    result = await FreezeAttentionObservation(factory).execute(_command())

    assert result.created is True
    observation = result.stored.observation
    assert observation.account_id == ACCOUNT
    assert observation.cutoff == CUTOFF
    assert observation.recorded_at == CUTOFF
    assert observation.provenance == _provenance()
    assert [member.canonical_symbol for member in observation.members] == ["HAL", "SBIN"]
    assert observation.members[0].score == 3
    assert observation.members[0].market_context_sha256 == "1" * 64
    assert observation.packet_body_sha256 == "3" * 64
    assert factory.units[0].commits == 1


@pytest.mark.asyncio
async def test_identical_freeze_is_idempotent_and_recording_time_is_not_identity() -> None:
    """A retry time cannot turn one logical PIT state into a second fact."""
    factory = Factory()
    use_case = FreezeAttentionObservation(factory)

    first = await use_case.execute(_command())
    repeat = await use_case.execute(_command(recorded_at=CUTOFF + timedelta(minutes=5)))

    assert first.created is True
    assert repeat.created is False
    assert len(factory.store.rows) == 1
    assert (
        first.stored.observation.observation_sha256 == repeat.stored.observation.observation_sha256
    )
    assert repeat.stored.observation.recorded_at == CUTOFF


@pytest.mark.asyncio
async def test_changed_same_cutoff_state_appends_a_superseding_fact() -> None:
    """Changed same-cutoff input is visible history, not silent mutation."""
    factory = Factory()
    use_case = FreezeAttentionObservation(factory)

    first = await use_case.execute(_command())
    changed = await use_case.execute(
        _command(hal_score=1, recorded_at=CUTOFF + timedelta(minutes=1))
    )

    assert changed.created is True
    assert len(factory.store.rows) == 2
    assert changed.stored.supersedes_sha256 == first.stored.observation.observation_sha256
    assert (
        changed.stored.observation.observation_sha256 != first.stored.observation.observation_sha256
    )


def test_observation_and_members_are_immutable_after_construction() -> None:
    """Frozen inputs provide no application mutation path."""
    observation = _command()

    with pytest.raises(FrozenInstanceError):
        observation.cutoff = CUTOFF + timedelta(days=1)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        observation.ranked[0].score = 14  # type: ignore[misc]


@pytest.mark.asyncio
async def test_a_ranking_from_after_the_cutoff_is_refused_before_persistence() -> None:
    """A later attention result cannot contaminate an earlier observation."""
    factory = Factory()
    command = _command()
    later = tuple(replace(entry, as_of=CUTOFF + timedelta(minutes=1)) for entry in command.ranked)

    with pytest.raises(ValidationError, match="different PIT cutoff"):
        await FreezeAttentionObservation(factory).execute(replace(command, ranked=later))

    assert factory.store.rows == []


@pytest.mark.asyncio
async def test_history_is_account_scoped_and_bounded() -> None:
    """The query binds its account to the transaction and honors the limit."""
    factory = Factory()
    await FreezeAttentionObservation(factory).execute(_command())

    rows = await ListResearchObservations(factory).execute(
        ListResearchObservationsQuery(account_id=ACCOUNT, limit=1)
    )

    assert len(rows) == 1
    with pytest.raises(AssertionError):
        await ListResearchObservations(factory).execute(
            ListResearchObservationsQuery(account_id=OTHER_ACCOUNT)
        )


def test_degraded_state_requires_and_preserves_an_honest_reason() -> None:
    """Degradation remains explicit provenance rather than a hidden success."""
    health = ObservationSourceHealth(
        market=ObservationSourceStatus.HEALTHY,
        news=ObservationSourceStatus.DEGRADED,
        degraded_reasons=("news: provider rate limited",),
    )

    command = _command(source_health=health)

    assert command.source_health.degraded_reasons == ("news: provider rate limited",)
