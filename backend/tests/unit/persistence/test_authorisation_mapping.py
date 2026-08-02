"""Tenant roles round-trip through record, mapper and factory boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.platform.domain.identity import Permission, PermissionGrant, Role
from dhruva.contexts.platform.infrastructure.persistence.factories import RoleFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_principal_role_model_kwargs,
    to_principal_role_record,
    to_role_model_kwargs,
    to_role_permission_model_kwargs,
    to_role_permission_record,
    to_role_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalRoleModel,
    RoleModel,
    RolePermissionModel,
)
from dhruva.contexts.platform.infrastructure.persistence.records import (
    PrincipalRoleRecord,
    RolePermissionRecord,
    RoleRecord,
)
from dhruva.shared.errors import DataQualityError
from dhruva.shared.identity import AccountId, PrincipalId

pytestmark = pytest.mark.unit

ROLES: Final = RoleFactory()
ACCOUNT: Final = AccountId.deterministic("authorisation-mapping")
PRINCIPAL: Final = PrincipalId.new()
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)


def _role(*permissions: Permission) -> Role:
    grants = frozenset(
        PermissionGrant(
            permission=permission,
            granted_at=CREATED + timedelta(minutes=5),
            granted_by="security-admin",
        )
        for permission in permissions
    )
    return Role(
        account_id=ACCOUNT,
        name="operator",
        created_at=CREATED,
        updated_at=CREATED + timedelta(minutes=5) if grants else CREATED,
        grants=grants,
        version=2 if grants else 1,
    )


def _round_trip(role: Role) -> Role:
    role_record, permission_records = ROLES.deconstruct(role)
    role_model = RoleModel(**to_role_model_kwargs(role_record))
    permission_models = [
        RolePermissionModel(**to_role_permission_model_kwargs(record))
        for record in permission_records
    ]
    return ROLES.reconstruct(
        to_role_record(role_model),
        tuple(to_role_permission_record(model) for model in permission_models),
    )


def test_an_empty_role_survives_the_complete_mapping_round_trip() -> None:
    """Deny-by-default's most important state is representable and lossless."""
    original = _role()

    assert _round_trip(original) == original


def test_permission_evidence_survives_the_complete_mapping_round_trip() -> None:
    """The live grant retains its value, actor and instant."""
    original = _role(Permission.PLACE_ORDER)

    assert _round_trip(original) == original


def test_an_unknown_stored_permission_is_bad_data_not_an_implicit_grant() -> None:
    """Fail closed when a row was written by code with a different vocabulary."""
    role_record, _ = ROLES.deconstruct(_role())
    unknown = RolePermissionRecord(
        account_id=ACCOUNT.value,
        role_name="operator",
        permission="all_access",
        granted_at=CREATED,
        granted_by="security-admin",
    )

    with pytest.raises(DataQualityError, match="unknown permission"):
        ROLES.reconstruct(role_record, (unknown,))


def test_a_permission_row_for_another_tenant_is_refused() -> None:
    """A factory never silently attaches a cross-tenant child row."""
    role_record, _ = ROLES.deconstruct(_role())
    wrong_tenant = RolePermissionRecord(
        account_id=AccountId.deterministic("someone-else").value,
        role_name="operator",
        permission=Permission.PLACE_ORDER.value,
        granted_at=CREATED,
        granted_by="security-admin",
    )

    with pytest.raises(DataQualityError, match="another role"):
        ROLES.reconstruct(role_record, (wrong_tenant,))


def test_duplicate_permission_rows_are_refused_instead_of_collapsed() -> None:
    """Two stored live grants for one permission are corrupt evidence, not one grant."""
    role_record, permission_records = ROLES.deconstruct(_role(Permission.PLACE_ORDER))
    duplicate = permission_records[0]

    with pytest.raises(DataQualityError, match="duplicate live grant"):
        ROLES.reconstruct(role_record, (duplicate, duplicate))


def test_principal_assignment_mapping_is_lossless() -> None:
    """The future assignment use case cannot lose its tenant or accountable actor."""
    original = PrincipalRoleRecord(
        principal_id=PRINCIPAL.value,
        account_id=ACCOUNT.value,
        role_name="operator",
        assigned_at=CREATED,
        assigned_by="security-admin",
    )
    model = PrincipalRoleModel(**to_principal_role_model_kwargs(original))

    assert to_principal_role_record(model) == original


@pytest.mark.parametrize(
    ("record", "model"),
    [(RoleRecord, RoleModel), (RolePermissionRecord, RolePermissionModel)],
)
def test_record_fields_match_model_columns(
    record: type[RoleRecord] | type[RolePermissionRecord],
    model: type[RoleModel] | type[RolePermissionModel],
) -> None:
    """A schema field cannot be added without crossing the mapping boundary."""
    columns = {column.name for column in model.__table__.columns}

    assert columns == set(record.__dataclass_fields__)
