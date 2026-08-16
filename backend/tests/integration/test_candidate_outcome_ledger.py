"""Matured candidate outcomes are tenant-bound, idempotent, and immutable."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.candidate_observations import (
    FreezeCandidateObservation,
    FreezeCandidateObservationCommand,
)
from dhruva.contexts.intelligence.domain.candidate_outcome import (
    CandidateOutcome,
    candidate_outcome_fingerprint,
)
from dhruva.contexts.intelligence.domain.candidates import (
    CandidateEligibility,
    CandidateRanking,
    CandidateResult,
    CandidateTier,
    EvidenceCompleteness,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    CandidateOutcomeModel,
    CandidateRankingObservationModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId
from dhruva.tooling.boundaries import find_repo_root

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER: Final = AccountId.deterministic("other-family")
CUTOFF: Final = datetime(2026, 1, 2, 10, tzinfo=UTC)
INSTRUMENT: Final = InstrumentId.deterministic("reference", "hal")
RESTRICT_VIOLATION: Final = "23001"


def _alembic_config() -> Config:
    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    return config


def _run_alembic(command: str, revision: str) -> None:
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    getattr(alembic_command, command)(_alembic_config(), revision)


def _check_alembic_drift() -> None:
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    alembic_command.check(_alembic_config())


def _ranking() -> CandidateRanking:
    return CandidateRanking(
        cutoff=CUTOFF,
        entries=(
            CandidateResult(
                instrument_id=INSTRUMENT,
                canonical_symbol="HAL",
                company_name="Hindustan Aeronautics Limited",
                cutoff=CUTOFF,
                eligibility=CandidateEligibility.ELIGIBLE,
                score=Decimal("25.00"),
                rank=1,
                universe_percentile=Decimal("50.0"),
                relative_strength_60_percentile=Decimal("50.0"),
                relative_strength_120_percentile=Decimal("50.0"),
                tier=CandidateTier.CANDIDATE,
                evidence_completeness=EvidenceCompleteness.LOW,
                supporting_evidence=(),
                counterevidence=(),
                missing_evidence=("adjustment UNKNOWN",),
                feature_availability=(("RETURN_120", "AVAILABLE"),),
            ),
        ),
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        feature_revision="technical-features-v0",
        limitations=("diagnostic",),
    )


def _sessions(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


async def _freeze(connection: AsyncConnection) -> None:
    sessions = _sessions(connection)
    await FreezeCandidateObservation(
        lambda account: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account)
    ).execute(
        FreezeCandidateObservationCommand(
            account_id=ACCOUNT,
            ranking=_ranking(),
            recorded_at=CUTOFF,
        )
    )


async def _fact(connection: AsyncConnection) -> CandidateOutcome:
    observation_id = await connection.scalar(select(CandidateRankingObservationModel.id))
    assert observation_id is not None
    observation_sha = await connection.scalar(
        select(CandidateRankingObservationModel.observation_sha256)
    )
    assert observation_sha is not None
    draft = CandidateOutcome(
        account_id=ACCOUNT,
        candidate_observation_id=observation_id,
        candidate_observation_sha256=observation_sha,
        instrument_id=INSTRUMENT,
        canonical_symbol="HAL",
        signal_cutoff=CUTOFF,
        observable_through=date(2026, 1, 22),
        materialized_at=datetime(2026, 1, 22, 12, tzinfo=UTC),
        horizon_sessions=20,
        entry_date=date(2026, 1, 3),
        entry_price=Decimal(100),
        exit_date=date(2026, 1, 22),
        exit_price=Decimal(110),
        absolute_return=Decimal("0.10"),
        benchmark_return=Decimal("0.03"),
        excess_return=Decimal("0.07"),
        net_return=Decimal("0.098"),
        net_excess_return=Decimal("0.068"),
        maximum_adverse_excursion=Decimal("-0.05"),
        maximum_favorable_excursion=Decimal("0.12"),
        holding_period_drawdown=Decimal("-0.08"),
        realized_volatility=Decimal("0.20"),
        ranker_revision="technical-candidate-v0",
        feature_revision="technical-features-v0",
        evaluation_revision="technical-candidate-evaluation-v0",
        outcome_revision="candidate-outcome-v0",
        benchmark_symbol="NIFTY 50",
        benchmark_basis="PRICE_INDEX",
        execution_timing="next-session open to Nth-session close",
        cost_bps=Decimal(20),
        adjustment_status="UNKNOWN",
        limitations=("diagnostic only",),
        stock_bar_revisions=("1" * 64,),
        benchmark_bar_revisions=("2" * 64,),
        outcome_sha256="",
    )
    return replace(draft, outcome_sha256=candidate_outcome_fingerprint(draft))


async def test_outcome_append_is_idempotent_and_account_scoped(
    connection: AsyncConnection,
) -> None:
    """Identical retries produce one row and another account reads none."""
    await _freeze(connection)
    sessions = _sessions(connection)
    fact = await _fact(connection)
    async with SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=ACCOUNT) as unit_of_work:
        observations = await unit_of_work.candidate_observations.list_recent(limit=10)
        first = await unit_of_work.candidate_outcomes.append(fact)
        await unit_of_work.commit()
    async with SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=ACCOUNT) as unit_of_work:
        retry = await unit_of_work.candidate_outcomes.append(fact)
        rows = await unit_of_work.candidate_outcomes.list_all()
        await unit_of_work.commit()
    async with SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=OTHER) as unit_of_work:
        other_rows = await unit_of_work.candidate_outcomes.list_all()

    assert first.created is True
    assert len(observations) == 1
    assert observations[0].observation.ranking == _ranking()
    assert retry.created is False
    assert rows == (fact,)
    assert other_rows == ()
    assert await connection.scalar(select(func.count()).select_from(CandidateOutcomeModel)) == 1


async def test_outcome_refuses_update(connection: AsyncConnection) -> None:
    """Database guards reject rewriting a matured historical fact."""
    await _freeze(connection)
    sessions = _sessions(connection)
    fact = await _fact(connection)
    async with SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=ACCOUNT) as unit_of_work:
        await unit_of_work.candidate_outcomes.append(fact)
        await unit_of_work.commit()

    with pytest.raises(DBAPIError) as raised:
        await connection.execute(text("UPDATE candidate_outcome SET cost_bps=0"))

    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


async def test_outcome_migration_downgrades_reapplies_and_has_no_drift(
    migrated: AsyncEngine,
    sole_alembic_head: str,
) -> None:
    """Revision 0020 is reversible and restores the sole drift-free head."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0019_candidate_ranking")
    try:
        async with migrated.connect() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'candidate_outcome'"
                )
            )
        assert remaining == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == sole_alembic_head
    await asyncio.to_thread(_check_alembic_drift)
