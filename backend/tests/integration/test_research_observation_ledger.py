"""The research evidence clock against real PostgreSQL (ADR-058, ADR-078)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.domain.attention import AttentionBand
from dhruva.contexts.intelligence.domain.research_observation import (
    AttentionObservationMember,
    ObservationAppendResult,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationSourceStatus,
    ObservationType,
    ResearchObservation,
    observation_fingerprint,
    universe_fingerprint,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    AttentionObservationMemberModel,
    ResearchObservationModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.marketdata.application.daily_history import (
    IngestHistoricalDailyHistory,
    IngestHistoricalDailyHistoryCommand,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER_ACCOUNT: Final = AccountId.deterministic("other-family")
CUTOFF: Final = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
HAL: Final = InstrumentId.deterministic("reference", "hal")
SBIN: Final = InstrumentId.deterministic("reference", "sbin")
RESTRICT_VIOLATION: Final = "23001"


def _provenance(*, attention: str = "watchlist-attention-v1") -> ObservationProvenance:
    return ObservationProvenance(
        attention_revision=attention,
        digest_revision="watchlist-digest-v1",
        digest_policy_revision="watchlist-digest-v1",
        entity_linking_revision="entity-linking-v1",
        event_classification_revision="event-classification-v1",
        market_context_revision="market-context-v1",
        news_identity_revision="news-identity-v1",
        sentiment_revision="lexical-sentiment-v1",
        packet_schema_revision="dhruva.research-packet.v1",
    )


def _observation(  # noqa: PLR0913 - each parameter varies one durable identity axis
    *,
    account_id: AccountId = ACCOUNT,
    recorded_at: datetime = CUTOFF,
    hal_score: int = 3,
    packet_hash: str = "a" * 64,
    attention_revision: str = "watchlist-attention-v1",
    degraded: bool = False,
) -> ResearchObservation:
    members = (
        AttentionObservationMember(
            instrument_id=HAL,
            rank=1,
            canonical_symbol="HAL",
            company_name="Hindustan Aeronautics Limited",
            score=hal_score,
            band=AttentionBand.ELEVATED if hal_score == 3 else AttentionBand.NORMAL,
            reasons=("1-day absolute move 3%",),
            market_context_available=True,
            market_context_sha256="1" * 64,
            archived_news_revisions=("2" * 64,),
        ),
        AttentionObservationMember(
            instrument_id=SBIN,
            rank=2,
            canonical_symbol="SBIN",
            company_name="State Bank of India",
            score=0,
            band=AttentionBand.LOW,
            reasons=(),
            market_context_available=False,
            market_context_sha256="3" * 64,
            archived_news_revisions=(),
        ),
    )
    provenance = _provenance(attention=attention_revision)
    health = ObservationSourceHealth(
        market=ObservationSourceStatus.HEALTHY,
        news=ObservationSourceStatus.DEGRADED if degraded else ObservationSourceStatus.HEALTHY,
        degraded_reasons=("news: rate limited",) if degraded else (),
    )
    universe_sha = universe_fingerprint(members)
    fingerprint = observation_fingerprint(
        account_id=account_id,
        observation_type=ObservationType.ATTENTION_OBSERVATION,
        cutoff=CUTOFF,
        universe_sha256=universe_sha,
        packet_body_sha256=packet_hash,
        provenance=provenance,
        source_health=health,
        members=members,
    )
    return ResearchObservation(
        account_id=account_id,
        observation_type=ObservationType.ATTENTION_OBSERVATION,
        cutoff=CUTOFF,
        recorded_at=recorded_at,
        provenance=provenance,
        source_health=health,
        members=members,
        universe_sha256=universe_sha,
        observation_sha256=fingerprint,
        packet_body_sha256=packet_hash,
    )


def _factory(
    connection: AsyncConnection, account_id: AccountId
) -> SqlAlchemyIntelligenceUnitOfWork:
    # Production-shaped UoW commits release savepoints while the fixture keeps
    # its rollback-only outer transaction for isolation.
    sessions = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    return SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


async def _append(
    connection: AsyncConnection,
    observation: ResearchObservation,
) -> ObservationAppendResult:
    async with _factory(connection, observation.account_id) as unit_of_work:
        result = await unit_of_work.observations.append(observation)
        await unit_of_work.commit()
    return result


class _HistoricalSource:
    """Return old market dates with a truthful modern retrieval timestamp."""

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        raw = f"historical-{request.instrument_id}".encode()
        candles = tuple(
            DailyCandle(
                trading_date=trading_date,
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("99"),
                close=Decimal("101"),
                volume=1000,
                open_interest=None,
            )
            for trading_date in (date(2020, 1, 2), date(2020, 1, 3))
        )
        return DailyHistoryBatch(
            provider="zerodha",
            request=request,
            retrieved_at=CUTOFF + timedelta(hours=1),
            content_sha256=hashlib.sha256(raw).hexdigest(),
            raw_response=raw,
            adjustment_status=AdjustmentStatus.UNKNOWN,
            candles=candles,
        )


async def test_historical_backfill_cannot_rewrite_a_frozen_observation(
    connection: AsyncConnection,
) -> None:
    """Old market dates append separately from prospective research evidence."""
    stored = await _append(connection, _observation())
    benchmark = InstrumentId.deterministic("reference", "nse-index-nifty-50")
    requests = tuple(
        DailyHistoryRequest(
            instrument_id=instrument_id,
            instrument_kind=kind,
            source_instrument_id=token,
            from_date=date(2020, 1, 2),
            to_date=date(2020, 1, 3),
        )
        for instrument_id, kind, token in (
            (benchmark, MarketInstrumentKind.INDEX, 100),
            (HAL, MarketInstrumentKind.CASH_EQUITY, 200),
        )
    )
    sessions = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    await IngestHistoricalDailyHistory(
        _HistoricalSource(),
        lambda account_id: SqlAlchemyMarketDataUnitOfWork(sessions, account_id=account_id),
    ).execute(
        IngestHistoricalDailyHistoryCommand(
            account_id=ACCOUNT,
            requests=requests,
            benchmark_id=benchmark,
            completed_through=date(2026, 8, 8),
        )
    )
    async with _factory(connection, ACCOUNT) as unit_of_work:
        after = await unit_of_work.observations.list_recent(limit=1)

    assert len(after) == 1
    assert after[0].observation == stored.stored.observation


async def test_an_attention_observation_and_every_ranked_member_are_persisted(
    connection: AsyncConnection,
) -> None:
    """One header and the complete ranking survive a database round trip."""
    observation = _observation()

    result = await _append(connection, observation)

    assert result.created is True
    assert result.stored.observation == observation
    assert await connection.scalar(select(func.count()).select_from(ResearchObservationModel)) == 1
    assert (
        await connection.scalar(select(func.count()).select_from(AttentionObservationMemberModel))
        == 2
    )
    header = (
        await connection.execute(
            select(
                ResearchObservationModel.account_id,
                ResearchObservationModel.cutoff,
                ResearchObservationModel.attention_revision,
                ResearchObservationModel.packet_body_sha256,
            )
        )
    ).one()
    assert header.account_id == ACCOUNT.value
    assert header.cutoff == CUTOFF
    assert header.attention_revision == "watchlist-attention-v1"
    assert header.packet_body_sha256 == "a" * 64


async def test_identical_append_is_idempotent_in_the_database(connection: AsyncConnection) -> None:
    """The database-backed repository returns one fact for an identical retry."""
    observation = _observation()

    first = await _append(connection, observation)
    repeat = await _append(connection, observation)

    assert first.created is True
    assert repeat.created is False
    assert await connection.scalar(select(func.count()).select_from(ResearchObservationModel)) == 1


async def test_changed_revision_at_the_same_cutoff_appends_and_supersedes(
    connection: AsyncConnection,
) -> None:
    """A changed ruleset identity appends a same-stream correction."""
    original = _observation()
    changed = _observation(
        recorded_at=CUTOFF + timedelta(minutes=1),
        attention_revision="watchlist-attention-v2",
    )

    first = await _append(connection, original)
    second = await _append(connection, changed)

    assert second.created is True
    assert second.stored.supersedes_sha256 == first.stored.observation.observation_sha256
    assert await connection.scalar(select(func.count()).select_from(ResearchObservationModel)) == 2


async def test_history_repository_cannot_read_another_account(
    connection: AsyncConnection,
) -> None:
    """Repository predicates isolate household histories under permissive-v1 RLS."""
    await _append(connection, _observation())
    await _append(connection, _observation(account_id=OTHER_ACCOUNT))

    async with _factory(connection, ACCOUNT) as owner:
        owner_rows = await owner.observations.list_recent(limit=20)
    async with _factory(connection, OTHER_ACCOUNT) as other:
        other_rows = await other.observations.list_recent(limit=20)

    assert [row.observation.account_id for row in owner_rows] == [ACCOUNT]
    assert [row.observation.account_id for row in other_rows] == [OTHER_ACCOUNT]


async def test_degraded_source_health_round_trips_without_becoming_healthy(
    connection: AsyncConnection,
) -> None:
    """Degraded provenance remains degraded after reconstruction."""
    await _append(connection, _observation(degraded=True))

    async with _factory(connection, ACCOUNT) as unit_of_work:
        rows = await unit_of_work.observations.list_recent(limit=1)

    assert rows[0].observation.status.value == "DEGRADED"
    assert rows[0].observation.source_health.degraded_reasons == ("news: rate limited",)


async def _assert_mutation_refused(connection: AsyncConnection, statement: str) -> None:
    await _append(connection, _observation())
    with pytest.raises(DBAPIError) as raised:
        await connection.execute(text(statement))
    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


async def test_the_database_refuses_observation_update(connection: AsyncConnection) -> None:
    """Raw UPDATE cannot bypass immutable application types."""
    await _assert_mutation_refused(
        connection,
        "UPDATE research_observation SET run_status = 'DEGRADED'",
    )


async def test_the_database_refuses_observation_delete(connection: AsyncConnection) -> None:
    """Raw DELETE cannot erase prospective evidence."""
    await _assert_mutation_refused(connection, "DELETE FROM research_observation")


async def test_the_database_refuses_observation_truncate(connection: AsyncConnection) -> None:
    """TRUNCATE is guarded because row-level triggers cannot see it."""
    await _assert_mutation_refused(
        connection,
        "TRUNCATE research_observation, attention_observation_member",
    )


async def test_member_rows_are_guarded_independently(connection: AsyncConnection) -> None:
    """A ranked member cannot be rewritten while leaving its header unchanged."""
    await _assert_mutation_refused(
        connection,
        "UPDATE attention_observation_member SET score = 14",
    )


def _alembic_config() -> Config:
    from alembic.config import Config as AlembicConfig  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = AlembicConfig(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    return config


def _run_alembic(command: str, revision: str) -> None:
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    getattr(alembic_command, command)(_alembic_config(), revision)


def _check_alembic_drift() -> None:
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper

    alembic_command.check(_alembic_config())


async def test_the_ledger_migration_downgrades_reapplies_and_has_no_drift(
    migrated: AsyncEngine,
    sole_alembic_head: str,
) -> None:
    """Revision 0018 is reversible, returns to the sole head and matches metadata."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0017_credential_purpose")
    try:
        async with migrated.connect() as connection:
            tables = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                    "('research_observation', 'attention_observation_member')"
                )
            )
        assert tables == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        tables = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
                "('research_observation', 'attention_observation_member')"
            )
        )
    assert current == sole_alembic_head
    assert tables == 2
    await asyncio.to_thread(_check_alembic_drift)
