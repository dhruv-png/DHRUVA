"""Permission management through the real transaction, stores and audit log."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.application.identity import GrantPermissionUseCase
from dhruva.contexts.platform.domain.identity import (
    EncryptedSecret,
    PasswordHash,
    Permission,
    PermissionGrant,
    Principal,
    Role,
)
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.identity import PrometheusIdentityMetrics
from dhruva.contexts.platform.infrastructure.persistence.models import PrincipalRoleModel
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, PrincipalId
from dhruva.shared.observability import MetricsRegistry
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
SEALED: Final = EncryptedSecret(ciphertext=b"sealed", wrapped_data_key=b"wrapped")


def _uow(
    engine: AsyncEngine,
    account_id: AccountId | None = None,
) -> SqlAlchemyIdentityUnitOfWork:
    return SqlAlchemyIdentityUnitOfWork(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        clock=FrozenClock(NOW),
        account_id=account_id,
    )


def _principal(account_id: AccountId, subject: str, *, totp: bool = True) -> Principal:
    return Principal(
        principal_id=PrincipalId.new(),
        account_id=account_id,
        subject=subject,
        password_hash=PasswordHash("stored-hash"),
        created_at=NOW,
        updated_at=NOW,
        totp_secret=SEALED if totp else None,
    )


def _role(account_id: AccountId, name: str, *permissions: Permission) -> Role:
    return Role(
        account_id=account_id,
        name=name,
        created_at=NOW,
        updated_at=NOW,
        grants=frozenset(
            PermissionGrant(permission=permission, granted_at=NOW, granted_by="bootstrap")
            for permission in permissions
        ),
    )


async def _seed(
    engine: AsyncEngine,
    account_id: AccountId,
    *,
    holder_totp: bool,
) -> Principal:
    actor = _principal(account_id, f"manager-{uuid4().hex[:10]}@dhruva.local")
    holder = _principal(
        account_id,
        f"holder-{uuid4().hex[:10]}@dhruva.local",
        totp=holder_totp,
    )
    manager = _role(account_id, "manager", Permission.MANAGE_AUTHORISATION)
    target = _role(account_id, "operator")
    async with _uow(engine, account_id) as uow:
        await uow.principals.add(actor)
        await uow.principals.add(holder)
        await uow.roles.add(manager)
        await uow.roles.add(target)
        await uow.session.flush()
        uow.session.add_all(
            [
                PrincipalRoleModel(
                    principal_id=actor.principal_id.value,
                    account_id=account_id.value,
                    role_name=manager.name,
                    assigned_at=NOW,
                    assigned_by="bootstrap",
                ),
                PrincipalRoleModel(
                    principal_id=holder.principal_id.value,
                    account_id=account_id.value,
                    role_name=target.name,
                    assigned_at=NOW,
                    assigned_by="bootstrap",
                ),
            ]
        )
        await uow.commit()
    return actor


@pytest.mark.usefixtures("truncated_after_test")
async def test_grant_is_durable_with_version_and_audit_in_one_transaction(
    migrated: AsyncEngine,
) -> None:
    """The real UoW commits the CAS, grant evidence, audit row and outbox together."""
    account_id = AccountId.deterministic("permission-flow-success", uuid4().hex)
    actor = await _seed(migrated, account_id, holder_totp=True)
    correlation = uuid4()

    changed = await GrantPermissionUseCase(
        lambda scoped_account: _uow(migrated, scoped_account),
        clock=FrozenClock(NOW),
        metrics=PrometheusIdentityMetrics(MetricsRegistry()),
    ).execute(
        actor_id=actor.principal_id,
        account_id=account_id,
        role_name="operator",
        permission=Permission.PLACE_ORDER,
        correlation_id=correlation,
    )

    async with migrated.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT r.version, p.permission, p.granted_by "
                    "FROM role AS r JOIN role_permission AS p "
                    "ON p.account_id = r.account_id AND p.role_name = r.name "
                    "WHERE r.account_id = :account AND r.name = 'operator'"
                ),
                {"account": account_id.value},
            )
        ).one()
        audit = (
            await connection.execute(
                text(
                    "SELECT actor, action, outcome, subject FROM audit_log "
                    "WHERE correlation_id = :correlation"
                ),
                {"correlation": correlation},
            )
        ).one()

    assert changed.version == 2
    assert tuple(row) == (2, Permission.PLACE_ORDER.value, actor.subject)
    assert tuple(audit) == (
        actor.subject,
        "configuration_change",
        "succeeded",
        "authorisation;operation=grant;reason=completed;role=operator;permission=place_order",
    )


@pytest.mark.usefixtures("truncated_after_test")
async def test_refused_protected_grant_commits_audit_without_mutating_role(
    migrated: AsyncEngine,
) -> None:
    """A shared-role TOTP refusal is durable while the target aggregate stays unchanged."""
    account_id = AccountId.deterministic("permission-flow-refusal", uuid4().hex)
    actor = await _seed(migrated, account_id, holder_totp=False)
    correlation = uuid4()

    with pytest.raises(ConflictError):
        await GrantPermissionUseCase(
            lambda scoped_account: _uow(migrated, scoped_account),
            clock=FrozenClock(NOW),
            metrics=PrometheusIdentityMetrics(MetricsRegistry()),
        ).execute(
            actor_id=actor.principal_id,
            account_id=account_id,
            role_name="operator",
            permission=Permission.PLACE_ORDER,
            correlation_id=correlation,
        )

    async with migrated.connect() as connection:
        role = (
            await connection.execute(
                text(
                    "SELECT version, "
                    "(SELECT count(*) FROM role_permission AS p "
                    "WHERE p.account_id = r.account_id AND p.role_name = r.name) AS grants "
                    "FROM role AS r WHERE account_id = :account AND name = 'operator'"
                ),
                {"account": account_id.value},
            )
        ).one()
        audit = (
            await connection.execute(
                text("SELECT outcome, subject FROM audit_log WHERE correlation_id = :correlation"),
                {"correlation": correlation},
            )
        ).one()

    assert tuple(role) == (1, 0)
    assert audit.outcome == "failed"
    assert "reason=two_factor_not_enrolled" in audit.subject
