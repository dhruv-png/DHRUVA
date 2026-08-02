"""Role repository orchestration without substituting for PostgreSQL semantics."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Self, cast

import pytest

from dhruva.contexts.platform.domain.identity import Permission, PermissionGrant, Role
from dhruva.contexts.platform.infrastructure.persistence.authorisation import RoleRepository
from dhruva.contexts.platform.infrastructure.persistence.factories import RoleFactory
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalRoleModel,
    RoleModel,
    RolePermissionModel,
)
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId, PrincipalId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("authorisation-repository-unit")
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)


class _Result:
    """The small result surface ``RoleRepository`` consumes."""

    def __init__(
        self,
        *,
        one: object | None = None,
        many: tuple[object, ...] = (),
        rowcount: int = 1,
    ) -> None:
        self.one = one
        self.many = many
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> object | None:
        """Return the configured scalar."""
        return self.one

    def scalars(self) -> Self:
        """Preserve the configured scalar sequence."""
        return self

    def all(self) -> list[object]:
        """Return the configured scalar sequence."""
        return list(self.many)


class _Session:
    """Queue results and retain staged rows for outcome assertions."""

    def __init__(self, *results: _Result, got: object | None = None) -> None:
        self.results = list(results)
        self.got = got
        self.statements: list[object] = []
        self.added: list[object] = []

    async def execute(self, statement: object) -> _Result:
        """Return the next result in repository call order."""
        self.statements.append(statement)
        return self.results.pop(0)

    async def get(self, model: type[object], key: object) -> object | None:
        """Return the configured principal-role assignment."""
        del model, key
        return self.got

    def add(self, instance: object) -> None:
        """Retain one staged row."""
        self.added.append(instance)

    def add_all(self, instances: Iterable[object]) -> None:
        """Retain all staged rows, including generator inputs."""
        self.added.extend(instances)


def _repository(session: _Session) -> RoleRepository:
    return RoleRepository(cast("Any", session), RoleFactory())


def _role(*permissions: Permission, version: int = 1) -> Role:
    granted_at = CREATED + timedelta(minutes=1)
    grants = frozenset(
        PermissionGrant(
            permission=permission,
            granted_at=granted_at,
            granted_by="security-admin",
        )
        for permission in permissions
    )
    return Role(
        account_id=ACCOUNT,
        name="operator",
        created_at=CREATED,
        updated_at=granted_at if grants else CREATED,
        grants=grants,
        version=version,
    )


def _role_rows(role: Role) -> tuple[RoleModel, tuple[RolePermissionModel, ...]]:
    model = RoleModel(
        account_id=role.account_id.value,
        name=role.name,
        created_at=role.created_at,
        updated_at=role.updated_at,
        version=role.version,
    )
    grants = tuple(
        RolePermissionModel(
            account_id=role.account_id.value,
            role_name=role.name,
            permission=grant.permission.value,
            granted_at=grant.granted_at,
            granted_by=grant.granted_by,
        )
        for grant in role.grants
    )
    return model, grants


@pytest.mark.asyncio
async def test_get_returns_none_for_a_missing_role() -> None:
    """Missing is ordinary deny-by-default state."""
    repository = _repository(_Session(_Result(one=None)))

    assert await repository.get(ACCOUNT, "missing") is None


@pytest.mark.asyncio
async def test_get_reconstructs_the_role_and_its_live_grants() -> None:
    """The two persistence queries form one complete aggregate."""
    expected = _role(Permission.PLACE_ORDER, version=2)
    model, grants = _role_rows(expected)
    repository = _repository(_Session(_Result(one=model), _Result(many=grants)))

    assert await repository.get(ACCOUNT, expected.name) == expected


@pytest.mark.asyncio
async def test_get_for_principal_distinguishes_unassigned_and_assigned() -> None:
    """An absent assignment denies; a present one follows its tenant role key."""
    principal_id = PrincipalId.new()
    assert await _repository(_Session(got=None)).get_for_principal(principal_id) is None

    expected = _role()
    model, _ = _role_rows(expected)
    assignment = PrincipalRoleModel(
        principal_id=principal_id.value,
        account_id=ACCOUNT.value,
        role_name=expected.name,
        assigned_at=CREATED,
        assigned_by="security-admin",
    )
    repository = _repository(_Session(_Result(one=model), _Result(many=()), got=assignment))

    assert await repository.get_for_principal(principal_id) == expected


@pytest.mark.asyncio
async def test_add_stages_the_role_and_every_live_grant_without_committing() -> None:
    """Repository insertion owns row orchestration but not the transaction."""
    session = _Session()
    await _repository(session).add(_role(Permission.PLACE_ORDER, version=2))

    assert [type(row) for row in session.added] == [RoleModel, RolePermissionModel]


@pytest.mark.asyncio
async def test_update_replaces_grants_only_after_winning_compare_and_swap() -> None:
    """The role row advances before its security-sensitive child set changes."""
    session = _Session(_Result(rowcount=1), _Result())
    changed = _role(Permission.PLACE_ORDER, version=2)

    await _repository(session).update(changed)

    assert len(session.statements) == 2
    assert [type(row) for row in session.added] == [RolePermissionModel]


@pytest.mark.asyncio
async def test_update_refuses_a_stale_role_before_touching_its_grants() -> None:
    """A lost compare-and-swap never deletes the winner's permission rows."""
    session = _Session(_Result(rowcount=0))

    with pytest.raises(ConflictError, match="modified by another writer"):
        await _repository(session).update(_role(Permission.PLACE_ORDER, version=2))

    assert len(session.statements) == 1
    assert session.added == []
