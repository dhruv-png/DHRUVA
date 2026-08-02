"""Audited permission grants and revocations (ADR-073)."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote

from dhruva.contexts.platform.domain.audit import AuditAction, AuditOutcome, AuditRecord
from dhruva.contexts.platform.domain.identity.authorisation import (
    Permission,
    PermissionGrant,
    is_permitted,
    may_grant,
)
from dhruva.shared.errors import (
    ConflictError,
    DhruvaError,
    NotFoundError,
    PermissionDeniedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from dhruva.contexts.platform.domain.identity.authorisation import Role
    from dhruva.contexts.platform.domain.identity.ports import IdentityUnitOfWork
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.shared.identity import AccountId, PrincipalId
    from dhruva.shared.time import Clock

__all__ = ["GrantPermissionUseCase", "RevokePermissionUseCase"]


class _Operation(StrEnum):
    GRANT = "grant"
    REVOKE = "revoke"


class _PermissionMutation:
    """Shared fail-closed workflow for the two permission mutations."""

    __slots__ = ("_clock", "_unit_of_work_factory")

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
        *,
        clock: Clock,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    async def _execute(
        self,
        *,
        operation: _Operation,
        actor_id: PrincipalId,
        account_id: AccountId,
        role_name: str,
        permission: Permission,
        correlation_id: UUID,
    ) -> Role:
        now = self._clock.now()
        async with self._unit_of_work_factory(account_id) as uow:
            await uow.authorisation.serialise(account_id)
            actor = await uow.principals.get(actor_id)
            if actor is None:
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="actor_not_found",
                    actor=str(actor_id),
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )
            if actor.account_id != account_id:
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="cross_tenant_actor",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )
            if not actor.is_active:
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="disabled_actor",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )
            if not actor.is_two_factor_enrolled:
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="actor_totp_not_enrolled",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

            actor_role = await uow.roles.get_for_principal(actor_id)
            if (
                actor_role is None
                or actor_role.account_id != account_id
                or not is_permitted(actor_role, Permission.MANAGE_AUTHORISATION)
            ):
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="actor_lacks_management_permission",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

            target = await uow.roles.get(account_id, role_name)
            if target is None:
                await self._refuse(
                    uow,
                    error=NotFoundError("role does not exist"),
                    operation=operation,
                    reason="role_not_found",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

            if (
                operation is _Operation.GRANT
                and permission is Permission.MANAGE_AUTHORISATION
                and actor_role.name == target.name
            ):
                await self._refuse(
                    uow,
                    error=PermissionDeniedError("permission mutation refused"),
                    operation=operation,
                    reason="self_escalation",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

            if operation is _Operation.GRANT:
                changed = await self._grant(
                    uow,
                    actor=actor,
                    target=target,
                    permission=permission,
                    operation=operation,
                    account_id=account_id,
                    correlation_id=correlation_id,
                    now=now,
                )
            else:
                changed = await self._revoke(
                    uow,
                    actor=actor,
                    target=target,
                    permission=permission,
                    operation=operation,
                    account_id=account_id,
                    correlation_id=correlation_id,
                    now=now,
                )

            try:
                await uow.roles.update(changed)
            except ConflictError as error:
                await self._refuse(
                    uow,
                    error=error,
                    operation=operation,
                    reason="optimistic_concurrency_conflict",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=role_name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

            await self._record(
                uow,
                operation=operation,
                reason="completed",
                actor=actor.subject,
                outcome=AuditOutcome.SUCCEEDED,
                account_id=account_id,
                role_name=role_name,
                permission=permission,
                correlation_id=correlation_id,
                now=now,
            )
            await uow.commit()
            return changed

    async def _grant(
        self,
        uow: IdentityUnitOfWork,
        *,
        actor: Principal,
        target: Role,
        permission: Permission,
        operation: _Operation,
        account_id: AccountId,
        correlation_id: UUID,
        now: datetime,
    ) -> Role:
        if permission in target.granted:
            await self._refuse(
                uow,
                error=ConflictError("permission is already granted"),
                operation=operation,
                reason="permission_already_granted",
                actor=actor.subject,
                account_id=account_id,
                role_name=target.name,
                permission=permission,
                correlation_id=correlation_id,
                now=now,
            )

        for holder in await uow.authorisation.holders(account_id, target.name):
            verdict = may_grant(permission, totp_enrolled=holder.is_two_factor_enrolled)
            if not verdict.permitted:
                await self._refuse(
                    uow,
                    error=ConflictError("role holder does not satisfy grant requirements"),
                    operation=operation,
                    reason=verdict.refusal.value if verdict.refusal else "grant_refused",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=target.name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

        grant = PermissionGrant(permission=permission, granted_at=now, granted_by=actor.subject)
        return replace(
            target,
            grants=target.grants | {grant},
            updated_at=now,
            version=target.version + 1,
        )

    async def _revoke(
        self,
        uow: IdentityUnitOfWork,
        *,
        actor: Principal,
        target: Role,
        permission: Permission,
        operation: _Operation,
        account_id: AccountId,
        correlation_id: UUID,
        now: datetime,
    ) -> Role:
        if permission not in target.granted:
            await self._refuse(
                uow,
                error=ConflictError("permission is not granted"),
                operation=operation,
                reason="permission_not_granted",
                actor=actor.subject,
                account_id=account_id,
                role_name=target.name,
                permission=permission,
                correlation_id=correlation_id,
                now=now,
            )

        if permission is Permission.MANAGE_AUTHORISATION:
            remaining = await uow.authorisation.count_active_managers(
                account_id,
                excluding_role_name=target.name,
            )
            if remaining == 0:
                await self._refuse(
                    uow,
                    error=ConflictError("tenant must retain an authorised administrator"),
                    operation=operation,
                    reason="last_authorised_administrator",
                    actor=actor.subject,
                    account_id=account_id,
                    role_name=target.name,
                    permission=permission,
                    correlation_id=correlation_id,
                    now=now,
                )

        return replace(
            target,
            grants=frozenset(grant for grant in target.grants if grant.permission != permission),
            updated_at=now,
            version=target.version + 1,
        )

    @staticmethod
    async def _refuse(
        uow: IdentityUnitOfWork,
        *,
        error: DhruvaError,
        operation: _Operation,
        reason: str,
        actor: str,
        account_id: AccountId,
        role_name: str,
        permission: Permission,
        correlation_id: UUID,
        now: datetime,
    ) -> NoReturn:
        await _PermissionMutation._record(
            uow,
            operation=operation,
            reason=reason,
            actor=actor,
            outcome=AuditOutcome.FAILED,
            account_id=account_id,
            role_name=role_name,
            permission=permission,
            correlation_id=correlation_id,
            now=now,
        )
        await uow.commit()
        error.context.update(
            operation=operation.value,
            reason=reason,
            actor=actor,
            account_id=str(account_id),
            role=role_name,
            permission=permission.value,
        )
        raise error

    @staticmethod
    async def _record(
        uow: IdentityUnitOfWork,
        *,
        operation: _Operation,
        reason: str,
        actor: str,
        outcome: AuditOutcome,
        account_id: AccountId,
        role_name: str,
        permission: Permission,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        subject = (
            f"authorisation;operation={operation.value};reason={reason};"
            f"role={quote(role_name, safe='')};permission={permission.value}"
        )
        await uow.audit.record(
            AuditRecord(
                actor=actor,
                action=AuditAction.CONFIGURATION_CHANGE,
                subject=subject,
                outcome=outcome,
                occurred_at=now,
                recorded_at=now,
                account_id=account_id,
                correlation_id=correlation_id,
            )
        )


class GrantPermissionUseCase(_PermissionMutation):
    """Grant one named permission to a tenant role."""

    async def execute(
        self,
        *,
        actor_id: PrincipalId,
        account_id: AccountId,
        role_name: str,
        permission: Permission,
        correlation_id: UUID,
    ) -> Role:
        """Grant after authority, TOTP and concurrency checks."""
        return await self._execute(
            operation=_Operation.GRANT,
            actor_id=actor_id,
            account_id=account_id,
            role_name=role_name,
            permission=permission,
            correlation_id=correlation_id,
        )


class RevokePermissionUseCase(_PermissionMutation):
    """Revoke one named permission from a tenant role."""

    async def execute(
        self,
        *,
        actor_id: PrincipalId,
        account_id: AccountId,
        role_name: str,
        permission: Permission,
        correlation_id: UUID,
    ) -> Role:
        """Revoke without removing the tenant's last eligible manager."""
        return await self._execute(
            operation=_Operation.REVOKE,
            actor_id=actor_id,
            account_id=account_id,
            role_name=role_name,
            permission=permission,
            correlation_id=correlation_id,
        )
