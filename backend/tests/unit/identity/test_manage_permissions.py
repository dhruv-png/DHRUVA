"""Authority, safety, audit and concurrency rules for permission mutations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.application.identity import (
    GrantPermissionUseCase,
    RevokePermissionUseCase,
)
from dhruva.contexts.platform.domain.audit import AuditAction, AuditOutcome
from dhruva.contexts.platform.domain.identity import (
    AuthorisationOperation,
    EncryptedSecret,
    PasswordHash,
    Permission,
    PermissionGrant,
    Principal,
    Role,
    SecurityOutcome,
)
from dhruva.shared.errors import ConflictError, NotFoundError, PermissionDeniedError
from dhruva.shared.identity import AccountId, PrincipalId
from dhruva.shared.time import FrozenClock
from tests.unit.identity.fakes import FakeIdentityMetrics, FakeUnitOfWork

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
OTHER_ACCOUNT: Final = AccountId(UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
ACTOR_ID: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
HOLDER_ID: Final = PrincipalId(UUID("33333333-3333-3333-3333-333333333333"))
OTHER_MANAGER_ID: Final = PrincipalId(UUID("44444444-4444-4444-4444-444444444444"))
NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
SEALED: Final = EncryptedSecret(ciphertext=b"sealed", wrapped_data_key=b"wrapped")


def _principal(
    principal_id: PrincipalId,
    subject: str,
    *,
    account_id: AccountId = ACCOUNT,
    totp: bool = True,
    disabled: bool = False,
) -> Principal:
    return Principal(
        principal_id=principal_id,
        account_id=account_id,
        subject=subject,
        password_hash=PasswordHash("hashed-password"),
        created_at=NOW,
        updated_at=NOW,
        totp_secret=SEALED if totp else None,
        disabled_at=NOW if disabled else None,
    )


def _role(name: str, *permissions: Permission, account_id: AccountId = ACCOUNT) -> Role:
    grants = frozenset(
        PermissionGrant(permission=permission, granted_at=NOW, granted_by="bootstrap")
        for permission in permissions
    )
    return Role(
        account_id=account_id,
        name=name,
        created_at=NOW,
        updated_at=NOW,
        grants=grants,
    )


def _authorised_uow(*, target: Role | None = None) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    actor = uow.principals.seed(_principal(ACTOR_ID, "manager@dhruva.local"))
    manager = uow.roles.seed(_role("manager", Permission.MANAGE_AUTHORISATION))
    uow.roles.assign(actor.principal_id, manager)
    uow.roles.seed(target or _role("operator"))
    return uow


async def _grant(
    uow: FakeUnitOfWork,
    *,
    permission: Permission = Permission.PLACE_ORDER,
    role_name: str = "operator",
    metrics: FakeIdentityMetrics | None = None,
) -> Role:
    return await GrantPermissionUseCase(
        lambda _account_id: uow,
        clock=FrozenClock(NOW),
        metrics=metrics or FakeIdentityMetrics(),
    ).execute(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT,
        role_name=role_name,
        permission=permission,
        correlation_id=uuid4(),
    )


async def _revoke(
    uow: FakeUnitOfWork,
    *,
    permission: Permission = Permission.PLACE_ORDER,
    role_name: str = "operator",
    metrics: FakeIdentityMetrics | None = None,
) -> Role:
    return await RevokePermissionUseCase(
        lambda _account_id: uow,
        clock=FrozenClock(NOW),
        metrics=metrics or FakeIdentityMetrics(),
    ).execute(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT,
        role_name=role_name,
        permission=permission,
        correlation_id=uuid4(),
    )


def _assert_refusal(uow: FakeUnitOfWork, reason: str) -> None:
    assert uow.commits == 1
    assert uow.rollbacks == 0
    assert len(uow.audit.records) == 1
    record = uow.audit.records[0]
    assert record.action is AuditAction.CONFIGURATION_CHANGE
    assert record.outcome is AuditOutcome.FAILED
    assert f"reason={reason}" in record.subject
    assert "permission=" in record.subject


@pytest.mark.asyncio
async def test_grant_updates_role_version_and_commits_an_attributed_audit() -> None:
    """A successful grant changes one aggregate under the tenant lock."""
    uow = _authorised_uow()
    holder = uow.principals.seed(_principal(HOLDER_ID, "trader@dhruva.local"))
    target = uow.roles.by_key[(ACCOUNT.value, "operator")]
    uow.roles.assign(holder.principal_id, target)

    changed = await _grant(uow)

    assert changed.version == 2
    assert changed.granted == frozenset({Permission.PLACE_ORDER})
    assert next(iter(changed.grants)).granted_by == "manager@dhruva.local"
    assert uow.authorisation.serialised == [ACCOUNT]
    assert uow.commits == 1
    record = uow.audit.records[0]
    assert record.outcome is AuditOutcome.SUCCEEDED
    assert record.actor == "manager@dhruva.local"
    assert "operation=grant" in record.subject


@pytest.mark.asyncio
async def test_authorisation_metrics_cover_success_refusal_and_error_without_actor_data() -> None:
    """The metric vocabulary excludes actor, tenant, role, permission and refusal reason."""
    success = _authorised_uow()
    success_metrics = FakeIdentityMetrics()
    await _grant(success, metrics=success_metrics)

    refusal = _authorised_uow(target=_role("operator", Permission.PLACE_ORDER))
    refusal_metrics = FakeIdentityMetrics()
    with pytest.raises(ConflictError):
        await _grant(refusal, metrics=refusal_metrics)

    error = _authorised_uow()
    error.enter_error = RuntimeError("database unavailable")
    error_metrics = FakeIdentityMetrics()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await _revoke(error, metrics=error_metrics)

    assert success_metrics.authorisations == [
        (AuthorisationOperation.GRANT, SecurityOutcome.SUCCEEDED)
    ]
    assert refusal_metrics.authorisations == [
        (AuthorisationOperation.GRANT, SecurityOutcome.REFUSED)
    ]
    assert error_metrics.authorisations == [(AuthorisationOperation.REVOKE, SecurityOutcome.ERROR)]


@pytest.mark.asyncio
async def test_revoke_updates_role_without_retrying() -> None:
    """A successful revoke removes only the named grant and advances CAS once."""
    uow = _authorised_uow(
        target=_role("operator", Permission.PLACE_ORDER, Permission.MANAGE_AUTHORISATION)
    )

    changed = await _revoke(uow)

    assert changed.version == 2
    assert changed.granted == frozenset({Permission.MANAGE_AUTHORISATION})
    assert uow.commits == 1
    assert uow.audit.records[0].outcome is AuditOutcome.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "actor_not_found"),
        ("cross_tenant", "cross_tenant_actor"),
        ("disabled", "disabled_actor"),
        ("no_totp", "actor_totp_not_enrolled"),
        ("no_authority", "actor_lacks_management_permission"),
    ],
)
async def test_actor_preconditions_fail_closed_and_commit_refusal(
    mutation: str, reason: str
) -> None:
    """Identity, tenant, activity, TOTP and explicit authority are mandatory."""
    uow = _authorised_uow()
    if mutation == "missing":
        uow.principals.by_id.clear()
    elif mutation == "cross_tenant":
        uow.principals.seed(_principal(ACTOR_ID, "manager@dhruva.local", account_id=OTHER_ACCOUNT))
    elif mutation == "disabled":
        uow.principals.seed(_principal(ACTOR_ID, "manager@dhruva.local", disabled=True))
    elif mutation == "no_totp":
        uow.principals.seed(_principal(ACTOR_ID, "manager@dhruva.local", totp=False))
    else:
        uow.roles.seed(_role("manager"))

    with pytest.raises(PermissionDeniedError) as captured:
        await _grant(uow)

    assert captured.value.context["reason"] == reason
    _assert_refusal(uow, reason)


@pytest.mark.asyncio
async def test_missing_target_role_is_audited_before_typed_error() -> None:
    """A missing target is distinguished without losing the refusal audit."""
    uow = _authorised_uow()

    with pytest.raises(NotFoundError):
        await _grant(uow, role_name="missing")

    _assert_refusal(uow, "role_not_found")


@pytest.mark.asyncio
async def test_management_permission_cannot_be_granted_to_actors_current_role() -> None:
    """An actor cannot manufacture management authority on its own role."""
    uow = _authorised_uow()

    with pytest.raises(PermissionDeniedError):
        await _grant(
            uow,
            role_name="manager",
            permission=Permission.MANAGE_AUTHORISATION,
        )

    _assert_refusal(uow, "self_escalation")


@pytest.mark.asyncio
async def test_protected_grant_checks_every_holder_including_disabled_principals() -> None:
    """Disabled holders still need TOTP before a protected role grant."""
    uow = _authorised_uow()
    holder = uow.principals.seed(
        _principal(HOLDER_ID, "disabled-holder", totp=False, disabled=True)
    )
    target = uow.roles.by_key[(ACCOUNT.value, "operator")]
    uow.roles.assign(holder.principal_id, target)

    with pytest.raises(ConflictError):
        await _grant(uow)

    _assert_refusal(uow, "two_factor_not_enrolled")


@pytest.mark.asyncio
async def test_protected_permission_may_be_granted_to_an_empty_role() -> None:
    """An empty role has no current holder who can violate the TOTP rule."""
    uow = _authorised_uow()

    changed = await _grant(uow)

    assert Permission.PLACE_ORDER in changed.granted


@pytest.mark.asyncio
async def test_duplicate_grant_and_missing_revoke_are_audited_conflicts() -> None:
    """Both contradictory states are typed, durable refusals."""
    duplicate = _authorised_uow(target=_role("operator", Permission.PLACE_ORDER))
    with pytest.raises(ConflictError):
        await _grant(duplicate)
    _assert_refusal(duplicate, "permission_already_granted")

    missing = _authorised_uow()
    with pytest.raises(ConflictError):
        await _revoke(missing)
    _assert_refusal(missing, "permission_not_granted")


@pytest.mark.asyncio
async def test_last_authorised_administrator_cannot_be_removed() -> None:
    """The only eligible manager cannot remove its tenant's authority."""
    uow = _authorised_uow()

    with pytest.raises(ConflictError):
        await _revoke(
            uow,
            role_name="manager",
            permission=Permission.MANAGE_AUTHORISATION,
        )

    _assert_refusal(uow, "last_authorised_administrator")


@pytest.mark.asyncio
async def test_own_management_role_may_be_revoked_when_another_eligible_manager_remains() -> None:
    """Self-revocation is safe only when another role retains a manager."""
    uow = _authorised_uow()
    alternate = uow.principals.seed(_principal(OTHER_MANAGER_ID, "alternate-manager"))
    alternate_role = uow.roles.seed(_role("alternate", Permission.MANAGE_AUTHORISATION))
    uow.roles.assign(alternate.principal_id, alternate_role)

    changed = await _revoke(
        uow,
        role_name="manager",
        permission=Permission.MANAGE_AUTHORISATION,
    )

    assert Permission.MANAGE_AUTHORISATION not in changed.granted


@pytest.mark.asyncio
async def test_optimistic_conflict_is_not_retried_and_its_refusal_is_committed() -> None:
    """A stale role produces one audited refusal and no hidden retry."""
    uow = _authorised_uow()
    uow.roles.lose_next_update = True

    with pytest.raises(ConflictError, match="modified by another writer"):
        await _grant(uow)

    _assert_refusal(uow, "optimistic_concurrency_conflict")
