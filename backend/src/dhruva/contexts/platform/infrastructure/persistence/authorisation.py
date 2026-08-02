"""Role persistence with tenant identity and optimistic concurrency (ADR-073).

The repository owns only row orchestration. Permission names become domain
values in :class:`RoleFactory`, and transaction lifetime remains with the unit
of work. A principal without an assignment returns ``None``: deny-by-default is
ordinary authorisation state, not a missing-row exception.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, and_, delete, func, select, text, update

from dhruva.contexts.platform.domain.identity.authorisation import Permission
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_principal_record,
    to_role_model_kwargs,
    to_role_permission_model_kwargs,
    to_role_permission_record,
    to_role_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalModel,
    PrincipalRoleModel,
    RoleModel,
    RolePermissionModel,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.platform.domain.identity.authorisation import Role
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.infrastructure.persistence.factories import (
        PrincipalFactory,
        RoleFactory,
    )
    from dhruva.shared.identity import PrincipalId

__all__ = ["PostgresAuthorisationDirectory", "RoleRepository"]


class PostgresAuthorisationDirectory:
    """PostgreSQL-backed authority queries sharing the caller's transaction."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: PrincipalFactory) -> None:
        """Bind the directory to an active identity transaction."""
        self._session = session
        self._factory = factory

    async def serialise(self, account_id: AccountId) -> None:
        """Hold a deterministic tenant advisory lock until transaction end."""
        digest = hashlib.blake2b(
            account_id.value.bytes,
            digest_size=8,
            person=b"dhruva-authz",
        ).digest()
        lock_key = int.from_bytes(digest, byteorder="big", signed=True)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def holders(self, account_id: AccountId, role_name: str) -> tuple[Principal, ...]:
        """Return all role holders, including disabled principals."""
        statement = (
            select(PrincipalModel)
            .join(
                PrincipalRoleModel,
                and_(
                    PrincipalRoleModel.principal_id == PrincipalModel.id,
                    PrincipalRoleModel.account_id == PrincipalModel.account_id,
                ),
            )
            .where(
                PrincipalRoleModel.account_id == account_id.value,
                PrincipalRoleModel.role_name == role_name,
            )
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(self._factory.reconstruct(to_principal_record(model)) for model in models)

    async def count_active_managers(
        self,
        account_id: AccountId,
        *,
        excluding_role_name: str | None = None,
    ) -> int:
        """Count active, TOTP-enrolled principals holding management authority."""
        statement = (
            select(func.count(PrincipalModel.id))
            .select_from(PrincipalModel)
            .join(
                PrincipalRoleModel,
                and_(
                    PrincipalRoleModel.principal_id == PrincipalModel.id,
                    PrincipalRoleModel.account_id == PrincipalModel.account_id,
                ),
            )
            .join(
                RolePermissionModel,
                and_(
                    RolePermissionModel.account_id == PrincipalRoleModel.account_id,
                    RolePermissionModel.role_name == PrincipalRoleModel.role_name,
                ),
            )
            .where(
                PrincipalModel.account_id == account_id.value,
                PrincipalModel.disabled_at.is_(None),
                PrincipalModel.totp_secret.is_not(None),
                RolePermissionModel.permission == Permission.MANAGE_AUTHORISATION.value,
            )
        )
        if excluding_role_name is not None:
            statement = statement.where(PrincipalRoleModel.role_name != excluding_role_name)
        return int((await self._session.execute(statement)).scalar_one())


class RoleRepository:
    """Loads and stores one tenant's named role aggregates. Never commits."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: RoleFactory) -> None:
        """Bind the repository to a session and reconstruction factory."""
        self._session = session
        self._factory = factory

    async def get(self, account_id: AccountId, name: str) -> Role | None:
        """Return the named role in one tenant, including all live grants."""
        statement = select(RoleModel).where(
            RoleModel.account_id == account_id.value,
            RoleModel.name == name,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None

        grants = (
            (
                await self._session.execute(
                    select(RolePermissionModel).where(
                        RolePermissionModel.account_id == model.account_id,
                        RolePermissionModel.role_name == model.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        return self._factory.reconstruct(
            to_role_record(model),
            tuple(to_role_permission_record(grant) for grant in grants),
        )

    async def get_for_principal(self, principal_id: PrincipalId) -> Role | None:
        """Return the principal's single role, or ``None`` when unassigned."""
        assignment = await self._session.get(PrincipalRoleModel, principal_id.value)
        if assignment is None:
            return None
        return await self.get(AccountId(assignment.account_id), assignment.role_name)

    async def add(self, role: Role) -> None:
        """Stage a role and its live permission grants for insertion."""
        record, grants = self._factory.deconstruct(role)
        self._session.add(RoleModel(**to_role_model_kwargs(record)))
        self._session.add_all(
            RolePermissionModel(**to_role_permission_model_kwargs(grant)) for grant in grants
        )

    async def update(self, role: Role) -> None:
        """Replace a role's grants if its loaded version is still current.

        The role row is advanced first with a conditional update. Only its
        winner may replace the child grant set, so two administrators cannot
        silently combine or overwrite security-sensitive changes.
        """
        previous_version = role.version - 1
        record, grants = self._factory.deconstruct(role)
        statement = (
            update(RoleModel)
            .where(
                RoleModel.account_id == record.account_id,
                RoleModel.name == record.name,
                RoleModel.version == previous_version,
            )
            .values(updated_at=record.updated_at, version=record.version)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        if result.rowcount == 0:
            msg = "role was modified by another writer"
            raise ConflictError(
                msg,
                account_id=str(role.account_id),
                role=role.name,
                expected_version=previous_version,
            )

        await self._session.execute(
            delete(RolePermissionModel).where(
                RolePermissionModel.account_id == record.account_id,
                RolePermissionModel.role_name == record.name,
            )
        )
        self._session.add_all(
            RolePermissionModel(**to_role_permission_model_kwargs(grant)) for grant in grants
        )
