"""The append-only audit log, with mutation refused by the database.

Revision ID: 0008_audit_log
Revises: 0007_credential
Created: 2026-08-01

Reversibility: reversible
Rollback procedure: `alembic downgrade 0007_credential` drops the triggers, the
    trigger function, the policy and the table, in that order. The audit history
    goes with it -- and an audit trail that a rollback can erase is exactly the
    property ADR-071 exists to deny, so this rollback is an incident in its own
    right. Export the table before running it.
Irreversible operations: none structurally. The history loss above is a
    consequence of the drop.
Expected runtime: sub-second. CREATE TABLE and CREATE TRIGGER take no lock on
    anything existing, and the table is empty at creation.
Operational impact: none on deployment. Nothing writes to this table until the
    audit recorder is deployed against it. Note that once it does, `UPDATE` and
    `DELETE` will fail for every role including the owner -- that is the point,
    but it means a correction is a compensating row, never an edit.

Why a trigger rather than revoked grants
----------------------------------------
ADR-071 requires that the *database* enforce append-only, not the application.
Revoking `UPDATE` and `DELETE` from the application role would leave the table
owner and any superuser able to rewrite history, and the owner is who the
application connects as today. A `BEFORE` trigger refuses the operation
regardless of role, which is the only form of the guarantee that survives
someone connecting with psql at 09:20 to "just fix one row".

The trigger raises rather than returning NULL. Returning NULL from a `BEFORE`
trigger silently skips the row, which would make an attempted deletion look like
a successful one -- the same class of failure as an empty `downgrade()`.

Why `recorded_at` is separate from `occurred_at`
------------------------------------------------
`occurred_at` is when the action happened; `recorded_at` is when this row was
written. They differ whenever recording is deferred or replayed, and a log that
conflates them cannot answer "was this backdated?". ADR-006: both are timezone
aware, and nothing in this schema accepts a naive timestamp.

Why `metadata` is JSONB with a default
--------------------------------------
The action set grows -- ADR-071 has already gained `CREDENTIAL_READ` -- and each
action carries different detail. A JSONB column absorbs that without a migration
per action. `NOT NULL DEFAULT '{}'` rather than nullable, so that reading it
never needs a null check and "no detail" and "detail not recorded" are the same
representable thing. What must *not* go in it is the credential, the token, or
anything the redaction processors would strip from a log line (ADR-033).

Why the three indexes
---------------------
`account_id` because every tenant-scoped read filters on it and RLS will too;
`occurred_at` because the log is read as a time range far more often than by id;
`action` because "every CREDENTIAL_READ this week" is the question this table
exists to answer. No composite index yet -- adding one is cheap and guessing the
leading column before there is a query plan is not.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_audit_log"
down_revision: str | None = "0007_credential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_NAME = "audit_log_account_isolation"
GUARD_FUNCTION = "audit_log_reject_mutation"

#: `restrict_violation` is the closest SQLSTATE with a settled meaning, so a
#: caller can match on the code rather than on the message text.
_GUARD_SQL = f"""
CREATE FUNCTION {GUARD_FUNCTION}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (ADR-071): % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    """Create the audit table, its indexes, its policy and its append-only guard."""
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable: platform-level actions -- a failed login against no known
        # account, a scheduler run -- belong in the log and have no tenant.
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resource_type", sa.Text(), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_account", "audit_log", ["account_id"])
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY_NAME} ON audit_log USING (true)")

    op.execute(_GUARD_SQL)
    op.execute(
        f"CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
        f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()"
    )
    op.execute(
        f"CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()"
    )


def downgrade() -> None:
    """Reverse the migration, removing the guard before the table it guards."""
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON audit_log")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_index("ix_audit_log_account", table_name="audit_log")
    op.drop_table("audit_log")
