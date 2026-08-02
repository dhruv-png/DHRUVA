"""The authorisation tables say what ADR-073 decided, in the schema.

No database here. These assert the *shape* of the three tables against the
decisions ADR-073 records, so that a later change which quietly loosens one is a
failing test rather than a permission somebody did not know they had.

The behavioural half -- that a grant is refused without enrolled 2FA, that it is
audited, that a revoke reaches every holder -- arrives with the grant use cases.
What is assertable before them is that the schema cannot represent the states
those rules forbid.

Tables are reached through ``Base.metadata`` rather than through each model's
``__table__``. That is not a stylistic choice: it is what the Alembic drift check
reads, so looking them up this way also asserts they are registered. A table
created by a migration and absent from metadata is drift, and autogenerate would
propose dropping it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Table, UniqueConstraint

from dhruva.contexts.platform.domain.identity import Permission
from dhruva.contexts.platform.infrastructure.persistence.models import Base

pytestmark = pytest.mark.unit

ROLE = "role"
ROLE_PERMISSION = "role_permission"
PRINCIPAL_ROLE = "principal_role"
PRINCIPAL = "principal"


def table(name: str) -> Table:
    """Return a registered table, failing loudly if it is not in metadata."""
    assert name in Base.metadata.tables, (
        f"{name} is absent from metadata; autogenerate would propose dropping it"
    )
    return Base.metadata.tables[name]


def columns(name: str) -> set[str]:
    """Return the column names of a registered table."""
    return {column.name for column in table(name).columns}


def primary_key(name: str) -> list[str]:
    """Return the primary-key column names of a registered table, in order."""
    return [column.name for column in table(name).primary_key.columns]


# --------------------------------------------------------------------------- #
# One role per principal, enforced rather than intended
# --------------------------------------------------------------------------- #


def test_a_principal_can_hold_only_one_role() -> None:
    """The primary key is ``principal_id`` alone, so a second row is impossible.

    ADR-073's ``Role`` is singular and carries one name. Two rows per principal
    would make "which role does this principal hold?" a question with two
    answers and no rule for combining them -- and the natural wrong answer, union
    the permissions, is exactly how order placement becomes implied.
    """
    assert primary_key(PRINCIPAL_ROLE) == ["principal_id"]


def test_a_role_name_is_unique_within_a_tenant_not_across_the_platform() -> None:
    """Composite key, so two accounts may each have an "operator" (ADR-004)."""
    assert primary_key(ROLE) == ["account_id", "name"]


def test_a_role_is_optimistically_versioned() -> None:
    """Permission changes cannot silently overwrite another administrator's work."""
    version = table(ROLE).columns["version"]

    assert version.nullable is False
    assert str(version.server_default.arg) == "1"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Deny by default has to be storable
# --------------------------------------------------------------------------- #


def test_a_role_exists_independently_of_its_permissions() -> None:
    """A role granting nothing is a valid row, and it is the *default* state.

    It is what a new operator holds before anybody decides what they may do, and
    what a fully revoked operator falls back to. Inferring a role's existence
    from having at least one permission -- one table instead of two -- would make
    that state unstorable, which is why the two are separate.
    """
    assert "permission" not in columns(ROLE)
    assert ROLE_PERMISSION in Base.metadata.tables


def test_a_grant_is_the_row_itself_with_no_boolean_to_disagree_with_it() -> None:
    """One row means granted; no row means not.

    A ``granted`` boolean would give one fact two representations, and the
    dangerous disagreement runs in one direction: a row present with
    ``granted = false``, read by code that checks presence, is a permission
    somebody believes they revoked.
    """
    assert "granted" not in columns(ROLE_PERMISSION)
    assert primary_key(ROLE_PERMISSION) == ["account_id", "role_name", "permission"]


# --------------------------------------------------------------------------- #
# A grant points at a role that exists
# --------------------------------------------------------------------------- #


def test_a_permission_grant_cannot_outlive_the_role_it_names() -> None:
    """A permission child cascades from ``role``.

    A permission granted to a role nobody defined is a grant no query would find
    and no revoke would reach. Deleting a role is itself an audited configuration
    change; the cascade makes it complete rather than partial.
    """
    constraints = list(table(ROLE_PERMISSION).foreign_key_constraints)

    assert len(constraints) == 1
    assert constraints[0].referred_table.name == ROLE
    assert constraints[0].ondelete == "CASCADE"


def test_an_assignment_cannot_outlive_its_role_or_principal() -> None:
    """Both ends cascade, so an assignment can point at neither a ghost nor another tenant."""
    constraints = list(table(PRINCIPAL_ROLE).foreign_key_constraints)

    assert {constraint.referred_table.name for constraint in constraints} == {ROLE, PRINCIPAL}
    assert all(constraint.ondelete == "CASCADE" for constraint in constraints)


def test_the_principal_foreign_key_binds_identity_and_tenant_together() -> None:
    """A real principal cannot borrow a different account's role."""
    constraint = next(
        item
        for item in table(PRINCIPAL_ROLE).foreign_key_constraints
        if item.referred_table.name == PRINCIPAL
    )

    assert [(element.parent.name, element.column.name) for element in constraint.elements] == [
        ("principal_id", "id"),
        ("account_id", "account_id"),
    ]


def test_the_principal_exposes_the_composite_key_the_assignment_references() -> None:
    """PostgreSQL requires the tenant-safe foreign-key target to be unique."""
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table(PRINCIPAL).constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert ("id", "account_id") in unique_columns


# --------------------------------------------------------------------------- #
# Every table is tenant-scoped, so RLS has something to scope (ADR-004, ADR-074)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [ROLE, ROLE_PERMISSION, PRINCIPAL_ROLE])
def test_every_authorisation_table_carries_a_tenant(name: str) -> None:
    """ADR-004 requires it on every domain table and ADR-074 scopes policies by it.

    A table without one is a table S44 cannot activate isolation on, discovered
    at activation rather than here.
    """
    assert "account_id" in columns(name)


@pytest.mark.parametrize("name", [ROLE, ROLE_PERMISSION, PRINCIPAL_ROLE])
def test_no_authorisation_column_is_nullable(name: str) -> None:
    """Every column ADR-073 names is carried, not optional.

    A nullable ``role_name`` or ``permission`` would be a grant that names
    nothing -- representable, unqueryable, and impossible to revoke.
    """
    nullable = [column.name for column in table(name).columns if column.nullable]

    assert nullable == []


# --------------------------------------------------------------------------- #
# The permission value set stays the domain's to decide
# --------------------------------------------------------------------------- #


def test_the_permission_column_does_not_pin_the_enum_in_the_schema() -> None:
    """Text, not a database enum, for the reason ``audit_log.action`` records.

    The permission set grows -- plan §15.1 names order placement and nothing
    else, and later subsystems add their own -- and each growth would otherwise
    be an ``ALTER TYPE`` on a table holding live authorisation state. The domain
    enum is the authority; a check constraint listing the values would be a
    second list to keep in step with the first.
    """
    permission = table(ROLE_PERMISSION).columns["permission"]

    assert permission.type.__class__.__name__ == "Text"
    assert Permission.PLACE_ORDER.value == "place_order"


def test_the_accountable_actor_is_recorded_on_every_grant() -> None:
    """ADR-073 audits grants; these columns answer the *live* question.

    The audit log answers "what happened, in order". ``granted_by`` and
    ``assigned_by`` answer "who is responsible for the authority this principal
    holds right now", which is what is asked while looking at a permission rather
    than while reconstructing history.
    """
    assert "granted_by" in columns(ROLE_PERMISSION)
    assert "assigned_by" in columns(PRINCIPAL_ROLE)
