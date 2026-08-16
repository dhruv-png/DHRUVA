"""Bitemporal historical-universe persistence against PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.reference.api import (
    CorporateActionEvidence,
    CorporateActionType,
    EvidenceVerification,
    GetCorporateActions,
    GetHistoricalUniverse,
    HistoricalUniverseDefinition,
    HistoricalUniverseMembershipRevision,
    MembershipReason,
    RegisterCorporateActions,
    RegisterHistoricalUniverse,
    RegisterHistoricalUniverseCommand,
    SourceDiligenceStatus,
    UniverseKind,
)
from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
)
from dhruva.contexts.reference.infrastructure.persistence.unit_of_work import (
    SqlAlchemyReferenceUnitOfWork,
)
from dhruva.shared.errors import MissingDataError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
OTHER = AccountId.deterministic("other-family")
RENAMED = InstrumentId.deterministic("reference", "renamed-economic-history")
REMOVED = InstrumentId.deterministic("reference", "removed-delisted-history")
KNOWN = datetime(2010, 1, 2, 12, tzinfo=UTC)


def _run_alembic(command: str, revision: str) -> None:
    from alembic import command as alembic_command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)


def _factory(engine: AsyncEngine) -> Callable[[AccountId], SqlAlchemyReferenceUnitOfWork]:
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

    def create(account: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(sessions, account_id=account)

    return create


def _identity(  # noqa: PLR0913 - test builder exposes independent PIT axes
    instrument_id: InstrumentId,
    *,
    symbol: str,
    valid_from: date,
    valid_to: date | None,
    recorded_at: datetime,
    revision: str,
) -> InstrumentIdentityRevision:
    return InstrumentIdentityRevision(
        instrument_id=instrument_id,
        kind=InstrumentKind.EQUITY,
        canonical_symbol=symbol,
        company_name="Historical Example Limited",
        aliases=(),
        former_names=(),
        isin=None,
        sector="Test",
        concentration_groups=(),
        cash_mapping=CashInstrumentMapping(exchange="NSE", trading_symbol=symbol),
        futures_research_requested=False,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at,
        source="licensed-fixture",
        source_revision=revision,
    )


async def test_historical_membership_identity_and_known_at_are_pit_correct(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001
) -> None:
    """Future revisions do not leak; removals/delistings and renames retain identity."""
    factory = _factory(migrated)
    async with factory(ACCOUNT) as unit_of_work:
        assert await unit_of_work.reference.add_identity(
            _identity(
                RENAMED,
                symbol="OLDCO",
                valid_from=date(2000, 1, 1),
                valid_to=date(2019, 12, 31),
                recorded_at=KNOWN,
                revision="mapping-v1",
            )
        )
        assert await unit_of_work.reference.add_identity(
            _identity(
                RENAMED,
                symbol="NEWCO",
                valid_from=date(2020, 1, 1),
                valid_to=None,
                recorded_at=datetime(2019, 12, 1, 12, tzinfo=UTC),
                revision="mapping-v2",
            )
        )
        assert await unit_of_work.reference.add_identity(
            _identity(
                REMOVED,
                symbol="GONECO",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                recorded_at=KNOWN,
                revision="mapping-removed-v1",
            )
        )
        await unit_of_work.commit()

    definition = HistoricalUniverseDefinition(
        account_id=ACCOUNT,
        universe_id="licensed-india-v1",
        label="Licensed India PIT Universe",
        kind=UniverseKind.HISTORICAL_MARKET_UNIVERSE,
        known_at=datetime(2010, 2, 1, 12, tzinfo=UTC),
        source="licensed-fixture",
        source_revision="dataset-v1",
        source_status=SourceDiligenceStatus.TECHNICALLY_SUITABLE,
        historical_membership_available=True,
        removals_included=True,
        delistings_included=True,
        pit_known_at_available=True,
        instrument_lifecycle_available=True,
        licensing_confirmed=True,
    )
    memberships = (
        HistoricalUniverseMembershipRevision(
            account_id=ACCOUNT,
            universe_id=definition.universe_id,
            instrument_id=RENAMED,
            effective_from=date(2005, 1, 1),
            effective_to=None,
            known_at=definition.known_at,
            source=definition.source,
            source_revision=definition.source_revision,
            reason=MembershipReason.SOURCE_REPORTED,
            source_member_key="security-renamed",
        ),
        HistoricalUniverseMembershipRevision(
            account_id=ACCOUNT,
            universe_id=definition.universe_id,
            instrument_id=REMOVED,
            effective_from=date(2005, 1, 1),
            effective_to=date(2015, 12, 31),
            known_at=definition.known_at,
            source=definition.source,
            source_revision=definition.source_revision,
            reason=MembershipReason.SOURCE_REPORTED,
            source_member_key="security-removed",
            is_delisted=True,
        ),
    )
    command = RegisterHistoricalUniverseCommand(definition, memberships)
    use_case = RegisterHistoricalUniverse(factory)

    first = await use_case.execute(command)
    retry = await use_case.execute(command)
    past = await GetHistoricalUniverse(factory).execute(
        account_id=ACCOUNT,
        universe_id=definition.universe_id,
        effective_on=date(2015, 1, 1),
        known_at=datetime(2022, 1, 1, 12, tzinfo=UTC),
    )
    present = await GetHistoricalUniverse(factory).execute(
        account_id=ACCOUNT,
        universe_id=definition.universe_id,
        effective_on=date(2021, 1, 1),
        known_at=datetime(2022, 1, 1, 12, tzinfo=UTC),
    )

    assert first.definition_added
    assert first.memberships_added == 2
    assert not retry.definition_added
    assert retry.memberships_unchanged == 2
    assert [item.canonical_symbol for item in past.members] == ["GONECO", "OLDCO"]
    assert past.members[0].membership.is_delisted
    assert [item.canonical_symbol for item in present.members] == ["NEWCO"]
    assert present.members[0].membership.instrument_id == RENAMED
    assert present.survivorship.survivorship_safe

    action = CorporateActionEvidence(
        instrument_id=RENAMED,
        event_type=CorporateActionType.SPLIT,
        effective_date=date(2018, 6, 1),
        ex_date=date(2018, 6, 1),
        record_date=date(2018, 6, 4),
        known_at=datetime(2018, 5, 1, 12, tzinfo=UTC),
        source="licensed-fixture",
        source_revision="action-v1",
        verification=EvidenceVerification.VERIFIED,
        ratio_numerator=Decimal(2),
        ratio_denominator=Decimal(1),
    )
    action_use_case = RegisterCorporateActions(factory)
    assert (await action_use_case.execute(account_id=ACCOUNT, actions=(action,))).added == 1
    assert (await action_use_case.execute(account_id=ACCOUNT, actions=(action,))).unchanged == 1
    before_actions = await GetCorporateActions(factory).execute(
        account_id=ACCOUNT,
        instrument_id=RENAMED,
        effective_from=date(2018, 1, 1),
        effective_to=date(2018, 12, 31),
        known_at=datetime(2018, 4, 1, 12, tzinfo=UTC),
    )
    after_actions = await GetCorporateActions(factory).execute(
        account_id=ACCOUNT,
        instrument_id=RENAMED,
        effective_from=date(2018, 1, 1),
        effective_to=date(2018, 12, 31),
        known_at=datetime(2018, 7, 1, 12, tzinfo=UTC),
    )
    assert before_actions == ()
    assert after_actions == (action,)

    with pytest.raises(MissingDataError, match="knowledge cutoff"):
        await GetHistoricalUniverse(factory).execute(
            account_id=ACCOUNT,
            universe_id=definition.universe_id,
            effective_on=date(2015, 1, 1),
            known_at=datetime(2010, 1, 15, 12, tzinfo=UTC),
        )
    with pytest.raises(MissingDataError, match="knowledge cutoff"):
        await GetHistoricalUniverse(factory).execute(
            account_id=OTHER,
            universe_id=definition.universe_id,
            effective_on=date(2015, 1, 1),
            known_at=datetime(2022, 1, 1, 12, tzinfo=UTC),
        )


async def test_historical_evidence_migration_is_reversible(
    migrated: AsyncEngine, sole_alembic_head: str
) -> None:
    """Revision 0021 removes and restores all three evidence tables cleanly."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0020_candidate_outcome")
    try:
        async with migrated.connect() as connection:
            remaining = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('historical_universe_definition_revision', "
                    "'historical_universe_membership_revision', 'corporate_action_revision')"
                )
            )
        assert remaining == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")
    async with migrated.connect() as connection:
        current = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == sole_alembic_head
