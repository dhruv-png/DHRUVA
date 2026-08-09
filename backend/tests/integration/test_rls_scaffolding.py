"""RLS policy completeness and real PostgreSQL enforcement (ADR-074)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from dhruva.contexts.platform.infrastructure.database.tenant_context import (
    SESSION_ACCOUNT_SETTING,
)
from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

TENANT_TABLES: Final = frozenset(
    {
        "audit_log",
        "attention_observation_member",
        "credential",
        "daily_snapshot",
        "principal",
        "principal_role",
        "refresh_token",
        "research_observation",
        "role",
        "role_permission",
        "watchlist_membership_revision",
    }
)


async def _setting(session: AsyncSession) -> str | None:
    return cast(
        "str | None",
        await session.scalar(
            text("SELECT NULLIF(current_setting(:setting, true), '')"),
            {"setting": SESSION_ACCOUNT_SETTING},
        ),
    )


async def test_every_tenant_owned_table_has_enabled_rls_and_a_policy(
    connection: AsyncConnection,
) -> None:
    """Non-null ``account_id`` names tenant ownership; nullable outbox is provenance."""
    discovered = frozenset(
        (
            await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'account_id' "
                    "AND is_nullable = 'NO'"
                )
            )
        ).scalars()
    )
    enrolled = frozenset(
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_class AS c "
                    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relrowsecurity "
                    "AND EXISTS (SELECT 1 FROM pg_policies AS p "
                    "WHERE p.schemaname = n.nspname AND p.tablename = c.relname)"
                )
            )
        ).scalars()
    )

    assert discovered == TENANT_TABLES
    assert discovered <= enrolled


async def test_restrictive_policy_denies_missing_and_cross_tenant_context(
    connection: AsyncConnection,
) -> None:
    """Exercise isolation as a non-owner role while production remains permissive."""
    first, second = uuid4(), uuid4()
    role_name = f"dhruva_rls_test_{uuid4().hex}"
    await connection.execute(
        text(
            "INSERT INTO daily_snapshot "
            "(id, account_id, instrument_id, trading_day, close_scaled_units, "
            "turnover_minor_units, currency, version) VALUES "
            "(:first_id, :first, :first_instrument, DATE '2026-08-01', 1, 1, 'INR', 1), "
            "(:second_id, :second, :second_instrument, DATE '2026-08-01', 1, 1, 'INR', 1)"
        ),
        {
            "first_id": uuid4(),
            "first": first,
            "first_instrument": uuid4(),
            "second_id": uuid4(),
            "second": second,
            "second_instrument": uuid4(),
        },
    )
    await connection.execute(text("DROP POLICY daily_snapshot_account_isolation ON daily_snapshot"))
    await connection.execute(
        text(
            "CREATE POLICY daily_snapshot_account_isolation ON daily_snapshot "
            "USING (account_id = NULLIF(current_setting("
            "'dhruva.current_account_id', true), '')::uuid) "
            "WITH CHECK (account_id = NULLIF(current_setting("
            "'dhruva.current_account_id', true), '')::uuid)"
        )
    )
    await connection.execute(text(f'CREATE ROLE "{role_name}" NOLOGIN'))
    await connection.execute(text(f'GRANT SELECT ON daily_snapshot TO "{role_name}"'))
    await connection.execute(text(f'SET LOCAL ROLE "{role_name}"'))

    assert await connection.scalar(text("SELECT count(*) FROM daily_snapshot")) == 0
    await connection.execute(
        text("SELECT set_config(:setting, :account_id, true)"),
        {"setting": SESSION_ACCOUNT_SETTING, "account_id": str(first)},
    )
    visible = (
        (await connection.execute(text("SELECT account_id FROM daily_snapshot"))).scalars().all()
    )
    assert visible == [first]
    assert second not in visible


async def test_tenant_context_is_transaction_local_across_pool_reuse_and_rollback(
    migrated: AsyncEngine,  # noqa: ARG001 - requests the migrated schema
    database_url: str,
) -> None:
    """A pooled connection never carries a committed or rolled-back tenant forward."""
    pooled = create_async_engine(database_url, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(bind=pooled, expire_on_commit=False)
    account_id = AccountId(uuid4())
    try:
        async with SqlAlchemyUnitOfWork(factory, account_id=account_id) as uow:
            committed_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            assert await _setting(uow.session) == str(account_id.value)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert await uow.session.scalar(text("SELECT pg_backend_pid()")) == committed_pid
            assert await _setting(uow.session) is None
            await uow.rollback()

        async with SqlAlchemyUnitOfWork(factory, account_id=account_id) as uow:
            rolled_back_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            assert await _setting(uow.session) == str(account_id.value)
            await uow.rollback()

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert await uow.session.scalar(text("SELECT pg_backend_pid()")) == rolled_back_pid
            assert await _setting(uow.session) is None
    finally:
        await pooled.dispose()
