"""Tenant roles and principal assignments against real PostgreSQL (ADR-058)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from dhruva.contexts.platform.domain.identity import (
    EncryptedSecret,
    PasswordHash,
    Permission,
    PermissionGrant,
    Principal,
    Role,
)
from dhruva.contexts.platform.infrastructure.persistence.authorisation import (
    PostgresAuthorisationDirectory,
    RoleRepository,
)
from dhruva.contexts.platform.infrastructure.persistence.factories import (
    PrincipalFactory,
    RoleFactory,
)
from dhruva.contexts.platform.infrastructure.persistence.identity import PrincipalRepository
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalModel,
    PrincipalRoleModel,
    RoleModel,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, PrincipalId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ROLES: Final = RoleFactory()
PRINCIPALS: Final = PrincipalFactory()
ACCOUNT: Final = AccountId.deterministic("authorisation-store")
OTHER_ACCOUNT: Final = AccountId.deterministic("other-authorisation-store")
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
ENCODED: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
SEALED: Final = EncryptedSecret(ciphertext=b"sealed", wrapped_data_key=b"wrapped")


def _role(*, account_id: AccountId = ACCOUNT, name: str = "operator") -> Role:
    return Role(
        account_id=account_id,
        name=name,
        created_at=CREATED,
        updated_at=CREATED,
    )


def _principal(*, account_id: AccountId = ACCOUNT) -> Principal:
    return Principal(
        principal_id=PrincipalId.new(),
        account_id=account_id,
        subject=f"operator-{uuid4().hex[:12]}@dhruva.local",
        password_hash=PasswordHash(ENCODED),
        created_at=CREATED,
        updated_at=CREATED,
    )


def _assigned(principal: Principal, role: Role) -> PrincipalRoleModel:
    return PrincipalRoleModel(
        principal_id=principal.principal_id.value,
        account_id=role.account_id.value,
        role_name=role.name,
        assigned_at=CREATED,
        assigned_by="security-admin",
    )


async def test_a_role_and_its_permission_evidence_survive_a_round_trip(
    session: AsyncSession,
) -> None:
    """Every role and live-grant field returns exactly as written."""
    repository = RoleRepository(session, ROLES)
    original = Role(
        account_id=ACCOUNT,
        name="trader",
        created_at=CREATED,
        updated_at=CREATED + timedelta(minutes=1),
        grants=frozenset(
            {
                PermissionGrant(
                    permission=Permission.PLACE_ORDER,
                    granted_at=CREATED + timedelta(minutes=1),
                    granted_by="security-admin",
                )
            }
        ),
        version=2,
    )

    await repository.add(original)
    await session.flush()
    session.expunge_all()

    assert await repository.get(ACCOUNT, "trader") == original


async def test_an_unknown_role_returns_none(session: AsyncSession) -> None:
    """A missing role is ordinary deny-by-default state."""
    repository = RoleRepository(session, ROLES)

    assert await repository.get(ACCOUNT, "missing") is None


async def test_a_concurrent_role_update_is_refused(session: AsyncSession) -> None:
    """Only the writer holding the loaded version may replace grants."""
    repository = RoleRepository(session, ROLES)
    original = _role()
    await repository.add(original)
    await session.flush()

    grant = PermissionGrant(
        permission=Permission.PLACE_ORDER,
        granted_at=CREATED + timedelta(minutes=1),
        granted_by="security-admin",
    )
    changed = Role(
        account_id=original.account_id,
        name=original.name,
        created_at=original.created_at,
        updated_at=grant.granted_at,
        grants=frozenset({grant}),
        version=2,
    )
    await repository.update(changed)
    await session.flush()
    session.expunge_all()

    assert await repository.get(ACCOUNT, original.name) == changed

    stale = Role(
        account_id=original.account_id,
        name=original.name,
        created_at=original.created_at,
        updated_at=CREATED + timedelta(minutes=2),
        version=2,
    )
    with pytest.raises(ConflictError):
        await repository.update(stale)


async def test_an_unassigned_principal_has_no_role(session: AsyncSession) -> None:
    """No assignment means no authority rather than an infrastructure failure."""
    principal = _principal()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    await session.flush()

    assert await RoleRepository(session, ROLES).get_for_principal(principal.principal_id) is None


async def test_a_principal_role_is_loaded_with_its_permissions(session: AsyncSession) -> None:
    """The authorization read path follows the single assignment to its role."""
    principal = _principal()
    role = _role()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RoleRepository(session, ROLES)
    await repository.add(role)
    await session.flush()
    session.add(_assigned(principal, role))
    await session.flush()
    session.expunge_all()

    assert await repository.get_for_principal(principal.principal_id) == role


async def test_a_principal_cannot_hold_two_roles(session: AsyncSession) -> None:
    """The primary key enforces the approved one-role contract."""
    principal = _principal()
    first = _role(name="operator")
    second = _role(name="auditor")
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    repository = RoleRepository(session, ROLES)
    await repository.add(first)
    await repository.add(second)
    await session.flush()
    async with session.begin_nested():
        session.add_all([_assigned(principal, first), _assigned(principal, second)])

        with pytest.raises(IntegrityError):
            await session.flush()


async def test_a_principal_cannot_borrow_another_tenants_role(session: AsyncSession) -> None:
    """The composite principal foreign key makes cross-tenant assignment impossible."""
    principal = _principal(account_id=ACCOUNT)
    foreign_role = _role(account_id=OTHER_ACCOUNT)
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    await RoleRepository(session, ROLES).add(foreign_role)
    await session.flush()
    async with session.begin_nested():
        session.add(_assigned(principal, foreign_role))

        with pytest.raises(IntegrityError):
            await session.flush()


async def test_deleting_a_principal_cascades_its_live_assignment(session: AsyncSession) -> None:
    """Removing a principal cannot leave an authoritative orphan row."""
    principal = _principal()
    role = _role()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    await RoleRepository(session, ROLES).add(role)
    await session.flush()
    session.add(_assigned(principal, role))
    await session.flush()

    await session.execute(
        delete(PrincipalModel).where(PrincipalModel.id == principal.principal_id.value)
    )
    remaining = await session.scalar(
        select(PrincipalRoleModel).where(
            PrincipalRoleModel.principal_id == principal.principal_id.value
        )
    )

    assert remaining is None


async def test_deleting_a_role_cascades_its_live_assignment(session: AsyncSession) -> None:
    """Removing a role cannot leave a principal assigned to authority that no longer exists."""
    principal = _principal()
    role = _role()
    await PrincipalRepository(session, PRINCIPALS).add(principal)
    await RoleRepository(session, ROLES).add(role)
    await session.flush()
    session.add(_assigned(principal, role))
    await session.flush()

    await session.execute(
        delete(RoleModel).where(
            RoleModel.account_id == role.account_id.value,
            RoleModel.name == role.name,
        )
    )
    remaining = await session.scalar(
        select(PrincipalRoleModel).where(
            PrincipalRoleModel.principal_id == principal.principal_id.value
        )
    )

    assert remaining is None


async def test_authority_directory_counts_only_active_enrolled_managers(
    session: AsyncSession,
) -> None:
    """The PostgreSQL joins preserve eligibility and shared-role semantics."""
    manager = Role(
        account_id=ACCOUNT,
        name="manager",
        created_at=CREATED,
        updated_at=CREATED,
        grants=frozenset(
            {
                PermissionGrant(
                    permission=Permission.MANAGE_AUTHORISATION,
                    granted_at=CREATED,
                    granted_by="bootstrap",
                )
            }
        ),
    )
    eligible = _principal()
    eligible = replace(eligible, totp_secret=SEALED)
    disabled = _principal()
    disabled = replace(disabled, totp_secret=SEALED, disabled_at=CREATED)
    unenrolled = _principal()
    principals = PrincipalRepository(session, PRINCIPALS)
    roles = RoleRepository(session, ROLES)
    await roles.add(manager)
    for principal in (eligible, disabled, unenrolled):
        await principals.add(principal)
    await session.flush()
    session.add_all(_assigned(principal, manager) for principal in (eligible, disabled, unenrolled))
    await session.flush()

    directory = PostgresAuthorisationDirectory(session, PRINCIPALS)
    await directory.serialise(ACCOUNT)

    assert {holder.subject for holder in await directory.holders(ACCOUNT, manager.name)} == {
        eligible.subject,
        disabled.subject,
        unenrolled.subject,
    }
    assert await directory.count_active_managers(ACCOUNT) == 1
    assert await directory.count_active_managers(ACCOUNT, excluding_role_name=manager.name) == 0
