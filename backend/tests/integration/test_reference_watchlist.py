"""Owner watchlist persistence and bitemporal lookup against PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.reference.application.watchlist import (
    ConfigureReferenceUniverse,
    GetSharedWatchlist,
)
from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
)
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.contexts.reference.infrastructure.persistence.models import (
    InstrumentIdentityRevisionModel,
    ReferenceInstrumentModel,
    WatchlistMembershipRevisionModel,
)
from dhruva.contexts.reference.infrastructure.persistence.unit_of_work import (
    SqlAlchemyReferenceUnitOfWork,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
OTHER_ACCOUNT = AccountId.deterministic("other-family")
RECORDED = datetime(2026, 8, 2, 12, tzinfo=UTC)
EFFECTIVE = date(2026, 8, 2)


def _unit_of_work_factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyReferenceUnitOfWork]:
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def create(account_id: AccountId) -> SqlAlchemyReferenceUnitOfWork:
        return SqlAlchemyReferenceUnitOfWork(session_factory, account_id=account_id)

    return create


async def _counts(engine: AsyncEngine) -> tuple[int, int, int]:
    """Return stable identity, identity revision and membership row counts."""
    async with engine.connect() as connection:
        return (
            int(
                await connection.scalar(select(func.count()).select_from(ReferenceInstrumentModel))
                or 0
            ),
            int(
                await connection.scalar(
                    select(func.count()).select_from(InstrumentIdentityRevisionModel)
                )
                or 0
            ),
            int(
                await connection.scalar(
                    select(func.count()).select_from(WatchlistMembershipRevisionModel)
                )
                or 0
            ),
        )


def _run_alembic(command: str, revision: str) -> None:
    """Run one migration command in the worker thread Alembic requires."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper
    from alembic.config import Config  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    getattr(alembic_command, command)(config, revision)


def _check_alembic_drift() -> None:
    """Ask Alembic whether live schema and all context metadata agree."""
    from alembic import command as alembic_command  # noqa: PLC0415 - test helper
    from alembic.config import Config  # noqa: PLC0415 - test helper

    from dhruva.tooling.boundaries import find_repo_root  # noqa: PLC0415 - test helper

    root = find_repo_root()
    config = Config(str(root / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(root / "backend" / "alembic"))
    alembic_command.check(config)


async def test_owner_universe_round_trips_and_is_idempotent(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """Twenty equities share one watchlist while Nifty remains a separate identity."""
    factory = _unit_of_work_factory(migrated)
    configure = ConfigureReferenceUniverse(factory)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)

    first = await configure.execute(command)
    retry = await configure.execute(replace(command, recorded_at=RECORDED + timedelta(hours=1)))
    watchlist = await GetSharedWatchlist(factory).execute(
        account_id=ACCOUNT,
        effective_on=EFFECTIVE,
        known_at=RECORDED + timedelta(hours=2),
    )

    assert first.identities_added == 21
    assert first.memberships_added == 20
    assert retry.identities_unchanged == 21
    assert retry.memberships_unchanged == 20
    assert await _counts(migrated) == (21, 21, 20)
    symbols = tuple(item.identity.canonical_symbol for item in watchlist)
    assert len(symbols) == 20
    assert "NAM-INDIA" in symbols
    assert "M&M" in symbols
    assert "NIFTY 50" not in symbols


async def test_point_in_time_query_cannot_see_a_later_correction(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """A later alias revision is invisible at the earlier knowledge instant."""
    factory = _unit_of_work_factory(migrated)
    configure = ConfigureReferenceUniverse(factory)
    initial = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    await configure.execute(initial)
    corrected_at = RECORDED + timedelta(days=1)
    corrected_definitions = tuple(
        replace(item, aliases=("Eternal Food Platform",))
        if item.canonical_symbol == "ETERNAL"
        else item
        for item in initial.definitions
    )
    await configure.execute(
        replace(
            initial,
            definitions=corrected_definitions,
            recorded_at=corrected_at,
            source_revision="owner-watchlist-test-correction",
        )
    )

    before = await GetSharedWatchlist(factory).execute(
        account_id=ACCOUNT,
        effective_on=EFFECTIVE,
        known_at=RECORDED,
    )
    after = await GetSharedWatchlist(factory).execute(
        account_id=ACCOUNT,
        effective_on=EFFECTIVE,
        known_at=corrected_at,
    )

    before_eternal = next(item for item in before if item.identity.canonical_symbol == "ETERNAL")
    after_eternal = next(item for item in after if item.identity.canonical_symbol == "ETERNAL")
    assert before_eternal.identity.aliases == ()
    assert after_eternal.identity.aliases == ("Eternal Food Platform",)


async def test_source_revision_reuse_with_changed_content_fails_closed(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """An idempotency key cannot silently name two owner configurations."""
    factory = _unit_of_work_factory(migrated)
    configure = ConfigureReferenceUniverse(factory)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    await configure.execute(command)
    changed = replace(command.definitions[0], sector="Changed In Test")

    with pytest.raises(ConflictError, match="reused"):
        await configure.execute(
            replace(
                command,
                definitions=(changed, *command.definitions[1:]),
                recorded_at=RECORDED + timedelta(hours=1),
            )
        )

    assert await _counts(migrated) == (21, 21, 20)


async def test_watchlist_is_tenant_scoped(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """The other family account cannot read this account's shared configuration."""
    factory = _unit_of_work_factory(migrated)
    await ConfigureReferenceUniverse(factory).execute(
        load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    )

    other = await GetSharedWatchlist(factory).execute(
        account_id=OTHER_ACCOUNT,
        effective_on=EFFECTIVE,
        known_at=RECORDED,
    )

    assert other == ()


async def test_reference_unit_of_work_rolls_back_by_default(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
) -> None:
    """Leaving without commit persists neither stable identity nor its revision."""
    session_factory = async_sessionmaker(bind=migrated, expire_on_commit=False)
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    definition = command.definitions[0]
    instrument_id = InstrumentId.deterministic("reference", definition.identity_key)

    identity = InstrumentIdentityRevision(
        instrument_id=instrument_id,
        kind=definition.kind,
        canonical_symbol=definition.canonical_symbol,
        company_name=definition.company_name,
        aliases=definition.aliases,
        former_names=definition.former_names,
        isin=definition.isin,
        sector=definition.sector,
        concentration_groups=definition.concentration_groups,
        cash_mapping=CashInstrumentMapping(
            exchange="NSE", trading_symbol=definition.canonical_symbol
        ),
        futures_research_requested=True,
        valid_from=definition.effective_from,
        valid_to=definition.effective_to,
        recorded_at=RECORDED,
        source=command.source,
        source_revision=command.source_revision,
    )

    async with SqlAlchemyReferenceUnitOfWork(session_factory, account_id=ACCOUNT) as unit_of_work:
        await unit_of_work.reference.add_identity(identity)

    assert await _counts(migrated) == (0, 0, 0)


async def test_reference_migration_downgrades_and_reapplies_cleanly(
    migrated: AsyncEngine,
    sole_alembic_head: str,
) -> None:
    """Revision 0013 is reversible and restores exactly one current head."""
    await asyncio.to_thread(_run_alembic, "downgrade", "0012_rls_scaffolding")
    try:
        async with migrated.connect() as connection:
            remaining = await connection.scalar(
                sql_text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('reference_instrument', 'instrument_identity_revision', "
                    "'watchlist_membership_revision')"
                )
            )
        assert remaining == 0
    finally:
        await asyncio.to_thread(_run_alembic, "upgrade", "head")

    async with migrated.connect() as connection:
        current = await connection.scalar(sql_text("SELECT version_num FROM alembic_version"))
    assert current == sole_alembic_head


async def test_reference_metadata_has_no_autogenerate_drift(
    migrated: AsyncEngine,  # noqa: ARG001 - requests the migrated schema
) -> None:
    """The next unrelated migration will not propose changing this schema."""
    await asyncio.to_thread(_check_alembic_drift)
