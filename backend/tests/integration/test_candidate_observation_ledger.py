"""Experimental candidate freezes remain complete, idempotent, and immutable."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final, cast

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.candidate_observations import (
    FreezeCandidateObservation,
    FreezeCandidateObservationCommand,
)
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateRanking,
    CandidateResult,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    CandidateRankingObservationModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT: Final = AccountId.deterministic("owner-family")
CUTOFF: Final = datetime(2026, 8, 14, 10, tzinfo=UTC)
RESTRICT_VIOLATION: Final = "23001"


def _ranking() -> CandidateRanking:
    return CandidateRanking(
        cutoff=CUTOFF,
        entries=(
            CandidateResult(
                instrument_id=InstrumentId.deterministic("reference", "hal"),
                canonical_symbol="HAL",
                company_name="Hindustan Aeronautics Limited",
                cutoff=CUTOFF,
                eligibility=CandidateEligibility.ELIGIBLE,
                score=Decimal("25.00"),
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


def _service(connection: AsyncConnection) -> FreezeCandidateObservation:
    sessions = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return FreezeCandidateObservation(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account)
    )


async def test_candidate_freeze_is_complete_and_idempotent(connection: AsyncConnection) -> None:
    """An identical weekly retry preserves exactly one full payload."""
    command = FreezeCandidateObservationCommand(
        account_id=ACCOUNT,
        ranking=_ranking(),
        recorded_at=CUTOFF,
    )

    first = await _service(connection).execute(command)
    retry = await _service(connection).execute(command)

    assert first.created is True
    assert retry.created is False
    assert first.observation.observation_sha256 == retry.observation.observation_sha256
    assert (
        await connection.scalar(select(func.count()).select_from(CandidateRankingObservationModel))
        == 1
    )
    row = (
        await connection.execute(
            select(
                CandidateRankingObservationModel.account_id,
                CandidateRankingObservationModel.eligible_count,
                CandidateRankingObservationModel.payload,
            )
        )
    ).one()
    assert row.account_id == ACCOUNT.value
    assert row.eligible_count == 1
    entries = cast("list[dict[str, object]]", row.payload["entries"])
    assert entries[0]["feature_availability"] == {"RETURN_120": "AVAILABLE"}


async def test_candidate_freeze_refuses_mutation(connection: AsyncConnection) -> None:
    """Raw SQL cannot rewrite an official candidate result."""
    await _service(connection).execute(
        FreezeCandidateObservationCommand(
            account_id=ACCOUNT,
            ranking=_ranking(),
            recorded_at=CUTOFF,
        )
    )

    with pytest.raises(DBAPIError) as raised:
        await connection.execute(text("UPDATE candidate_ranking_observation SET eligible_count=0"))

    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION
