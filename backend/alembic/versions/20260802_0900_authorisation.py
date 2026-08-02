"""Roles, the permissions they grant, and which principal holds which (ADR-073).

Revision ID: 0011_authorisation
Revises: 0010_principal
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0010_principal` drops the policies and the
    three tables, children first. Every grant goes with them, so after a rollback
    no principal holds any permission and -- because authorisation denies by
    default -- every guarded operation refuses. That is the safe direction to
    fail, but it is a total loss of authorisation state and every role must be
    re-created and re-granted before anyone can place an order.
Irreversible operations: none structurally. The grant history above is lost with
    the tables; the audit log records each grant and revoke as a configuration
    change (ADR-071), so what was granted and by whom remains reconstructible
    even though the live state does not.
Expected runtime: sub-second. Three CREATE TABLEs, all empty at creation, and no
    lock on anything existing.
Operational impact: none on deployment. Nothing reads these tables until the
    grant use cases are deployed against them. Note the direction of failure:
    until a role is created and granted, every permission check answers no.

Why three tables and not one
---------------------------
ADR-073 fixes the vocabulary: authorisation is **role-based**, a role is a named
set of permissions, and a role acquires a permission "only by being granted it by
name". Each table is one of those nouns.

`role` exists separately from `role_permission` so that **a role that grants
nothing is representable**. Collapsing the two -- inferring a role's existence
from having at least one permission -- would make the empty role unstorable, and
a deny-by-default system needs it: it is what a newly created operator holds
before anybody decides what they may do, and it is what a fully revoked operator
falls back to.

`principal_role` carries `principal_id` as its **primary key**, which is how "one
role per principal" is enforced rather than merely intended. ADR-073's `Role` is
singular and carries one name; two rows per principal would make "which role does
this principal hold?" a question with two answers and no rule for combining them.
Widening that later is an additive migration; narrowing it after two rows exist
would mean choosing which grant to discard.

Why `permission` is TEXT and not a PostgreSQL ENUM
--------------------------------------------------
The same reasoning `audit_log.action` records. The permission set will grow --
plan §15.1 names order placement and nothing else, and later subsystems add their
own -- and each growth would otherwise be an `ALTER TYPE`. The domain enum is the
authority on which values are permitted; a migration is not the place to also
enforce it, and a check constraint listing them would be a second list to keep in
step with the first.

Why the foreign keys exist here when they do not elsewhere
-----------------------------------------------------------
`daily_snapshot`, `outbox`, `credential`, `audit_log` and `refresh_token` all
carry `account_id` as a bare UUID because there is no `account` table to point
at, and `refresh_token.principal_id` was left unconstrained to join that same
future sweep. These constraints are different: `role` exists, in this migration,
so an orphan `role_permission` is preventable now rather than later. A permission
granted to a role nobody defined is a grant no query would find and no revoke
would reach.

`ON DELETE CASCADE` on both children, so deleting a role cannot leave grants
behind. Deleting a role is itself an audited configuration change; the cascade
makes it complete rather than partial.

Why `granted_by` is stored on the grant
----------------------------------------
The audit log records every grant (ADR-071, ADR-073), so this is the second copy.
It is kept because the two answer different questions: the audit log answers "what
happened, in order", and this column answers "who is responsible for the authority
this principal holds *right now*" -- which is the question asked while looking at
a live permission, without reconstructing history.

Why a role is versioned, and an assignment binds principal plus tenant
-----------------------------------------------------------------------
Permissions are the mutable state of the role aggregate. ``version`` applies
ADR-057's optimistic-concurrency rule to that state, so two administrators
changing one role cannot silently overwrite each other.

``principal_role`` references both ``principal.id`` and
``principal.account_id``. Referencing the id alone would prevent an orphan but
would still allow a principal from account A to be assigned a role from account
B by writing B into the assignment row. The composite key makes that
cross-tenant state unrepresentable, and ``ON DELETE CASCADE`` removes the live
assignment when its principal is removed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_authorisation"
down_revision: str | None = "0010_principal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Policy names, one per table (ADR-074). Permissive in v1: they change no
#: result today and exist so that S44 inherits a complete set rather than an
#: archaeology project.
POLICIES: Sequence[tuple[str, str]] = (
    ("role", "role_account_isolation"),
    ("role_permission", "role_permission_account_isolation"),
    ("principal_role", "principal_role_account_isolation"),
)

#: Trims the same whitespace the domain's `.strip()` does. Bare `btrim(x)` strips
#: spaces only, so a tab-only name would satisfy a check meant to mirror it --
#: the defect migration 0008 records in full.
_NON_BLANK = r"btrim({column}, E' \t\n\r') <> ''"


def upgrade() -> None:
    """Create the three authorisation tables, their indexes and their policies."""
    op.create_table(
        "role",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # Composite, because a role name is unique within a tenant and not
        # across the platform: two accounts may each have an "operator".
        sa.PrimaryKeyConstraint("account_id", "name", name="pk_role"),
        sa.CheckConstraint(_NON_BLANK.format(column="name"), name="ck_role_name"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_role_timestamps"),
        sa.CheckConstraint("version >= 1", name="ck_role_version"),
    )

    op.create_table(
        "role_permission",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_name", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.Text(), nullable=False),
        # The key *is* the grant: one row means the role holds the permission,
        # no row means it does not. There is no "granted: false" state, because
        # deny-by-default already provides it and a boolean would give the same
        # fact two representations.
        sa.PrimaryKeyConstraint("account_id", "role_name", "permission", name="pk_role_permission"),
        sa.ForeignKeyConstraint(
            ("account_id", "role_name"),
            ("role.account_id", "role.name"),
            name="fk_role_permission_role",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_NON_BLANK.format(column="permission"), name="ck_role_permission_value"),
        sa.CheckConstraint(_NON_BLANK.format(column="granted_by"), name="ck_role_permission_actor"),
    )

    op.create_table(
        "principal_role",
        # Primary key on the principal alone: one role per principal, enforced
        # rather than intended. See the module docstring.
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_name", sa.Text(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("principal_id", name="pk_principal_role"),
        sa.ForeignKeyConstraint(
            ("account_id", "role_name"),
            ("role.account_id", "role.name"),
            name="fk_principal_role_role",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ("principal_id", "account_id"),
            ("principal.id", "principal.account_id"),
            name="fk_principal_role_principal",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_NON_BLANK.format(column="assigned_by"), name="ck_principal_role_actor"),
    )

    # "Who holds this role?" -- the query a revoke runs to find out who is about
    # to lose a permission. The primary key indexes the other direction only.
    op.create_index("ix_principal_role_role", "principal_role", ["account_id", "role_name"])

    for table, policy in POLICIES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {policy} ON {table} USING (true)")


def downgrade() -> None:
    """Reverse the migration, dropping children before the table they reference."""
    for table, policy in POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_principal_role_role", table_name="principal_role")
    op.drop_table("principal_role")
    op.drop_table("role_permission")
    op.drop_table("role")
