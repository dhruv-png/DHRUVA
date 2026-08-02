"""The authenticating principal: who logs in, and what proves it.

Revision ID: 0010_principal
Revises: 0009_refresh_token
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0009_refresh_token` drops the policy and
    the table. Every operator's credentials and TOTP enrolment go with it, so
    every principal must be re-created and re-enrolled before anyone can
    authenticate again. Outstanding refresh tokens survive in their own table but
    reference principals that no longer exist, so they should be revoked in the
    same maintenance window.
Irreversible operations: none structurally. The credential loss above is a
    consequence of the drop, and password hashes are one-way by construction --
    they cannot be recovered from a backup of anything but this table.
Expected runtime: sub-second. CREATE TABLE takes no lock on anything existing,
    and the table is empty at creation.
Operational impact: none on deployment. Nothing reads this table until the
    authentication use case is deployed against it.

Why a principal is not an account
---------------------------------
`account_id` is a **tenant** (ADR-004); a principal is **who acted**. In a
single-operator deployment they are one-to-one, which is exactly why the
distinction has to be made in the schema now rather than discovered later: a
column that means "tenant" in seven tables and "human" in this one is not a
column anybody can query. ADR-072 already separates them -- `TokenClaims` carries
`subject` *and* `account_id` -- and this table is where that separation becomes
storage.

`subject` is unique **globally**, not per account. Authentication is presented
with a subject and a password and nothing else; scoping uniqueness to an account
would make the lookup ambiguous at exactly the moment there is more than one.

Why the password hash is a column and the TOTP secret is ciphertext
-------------------------------------------------------------------
They are different kinds of thing and the schema should not pretend otherwise.

A password hash is **one-way**. Argon2's encoded form carries its own algorithm,
parameters and salt, and there is no operation that recovers the password from
it, so encrypting it would protect nothing that is not already protected and
would add a key-custody problem to a column that has none.

A TOTP secret is **shared symmetric key material**: the server must be able to
recompute the same codes the operator's authenticator does, so it cannot be
hashed. That makes it exactly the kind of value ADR-070 exists for, and it is
stored the same way a broker credential is -- AES-256-GCM ciphertext plus a
wrapped data key, master key in process configuration, never in this table.

The pair is constrained to be present or absent together. A row holding
ciphertext with no wrapped key is one whose TOTP secret can never be opened,
and it would be discovered at the worst moment: when somebody is trying to log
in.

Why enrolment is derived rather than stored
-------------------------------------------
There is no `totp_enrolled` boolean. Enrolment *is* `totp_secret IS NOT NULL`,
and a second column asserting the same fact is a column that can disagree with
it. ADR-073 gates the order permission on enrolment, so the two disagreeing
would mean granting order placement to an account with no working second factor
-- which is the precise failure that ADR-073 exists to make structural.

Why `version` and not append-only
---------------------------------
Unlike `audit_log`, a principal is a mutable aggregate: passwords change, TOTP
gets enrolled, accounts get disabled. So ADR-057 applies in full, and the
`version` column is what lets two concurrent password changes fail loudly rather
than one silently overwriting the other.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_principal"
down_revision: str | None = "0009_refresh_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_NAME = "principal_account_isolation"


def upgrade() -> None:
    """Create the principal table, its indexes and its policy."""
    op.create_table(
        "principal",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret", postgresql.BYTEA(), nullable=True),
        sa.Column("totp_wrapped_key", postgresql.BYTEA(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id", name="pk_principal"),
        sa.UniqueConstraint("subject", name="uq_principal_subject"),
        # Redundant with the primary key for uniqueness, but required as the
        # target of principal_role's composite foreign key. That key binds the
        # principal identity and tenant together, so an assignment cannot point
        # at a real principal while borrowing some other account's role.
        sa.UniqueConstraint("id", "account_id", name="uq_principal_id_account"),
        # The character set is explicit because bare `btrim(x)` strips spaces
        # only, which would let a tab-only subject satisfy a check meant to
        # mirror the domain's `.strip()`. Migration 0008 records the full note.
        sa.CheckConstraint(r"btrim(subject, E' \t\n\r') <> ''", name="ck_principal_subject"),
        sa.CheckConstraint(
            r"btrim(password_hash, E' \t\n\r') <> ''", name="ck_principal_password_hash"
        ),
        sa.CheckConstraint("version >= 1", name="ck_principal_version"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_principal_timestamps"),
        # Ciphertext and wrapped key are only meaningful together. See the
        # module docstring: a row with one and not the other is a TOTP secret
        # nobody can open, discovered while somebody is trying to log in.
        sa.CheckConstraint(
            "(totp_secret IS NULL) = (totp_wrapped_key IS NULL)", name="ck_principal_totp_pair"
        ),
    )
    op.create_index("ix_principal_account", "principal", ["account_id"])

    op.execute("ALTER TABLE principal ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY_NAME} ON principal USING (true)")


def downgrade() -> None:
    """Reverse the migration."""
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON principal")
    op.execute("ALTER TABLE principal DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_principal_account", table_name="principal")
    op.drop_table("principal")
