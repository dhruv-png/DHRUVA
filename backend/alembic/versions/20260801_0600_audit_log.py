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
    audit recorder is deployed against it. Note that once it does, `UPDATE`,
    `DELETE` and `TRUNCATE` will fail for every role including the owner -- that
    is the point, but it means a correction is a compensating row, never an
    edit, and that no cleanup job can ever empty this table.

Why the columns are what they are
---------------------------------
They are the fields ADR-071 names, and they match
`dhruva.contexts.platform.domain.audit.AuditRecord` one for one. This revision
was first written with a different shape -- `actor_id UUID`, a
`resource_type`/`resource_id` pair, a `metadata JSONB` catch-all, a nullable
`account_id`, and no correlation id at all -- which disagreed with both the ADR
and the domain object in four ways at once. It is corrected here in place rather
than by a follow-up expand migration: this revision has never been applied
outside a test container, so there is no data the expand/contract discipline of
ADR-055 would be protecting, and shipping the wrong table plus a correction
would leave four columns nothing writes to and which ADR-071 then forbids
dropping.

Two of those differences were not cosmetic. `correlation_id` is required by
ADR-071 and is what joins an audit entry to the log lines and outbox rows of the
same request -- without it the log answers "what happened" but never "what else
happened at the same time". And `metadata JSONB` was a free-form field in the
one table that must never hold a secret: the domain module argues the case at
length, ADR-037's redaction strategies cannot help a value that was deliberately
stored, and a column that exists will eventually be filled.

`account_id` is NOT NULL because ADR-004 says every domain table carries one and
ADR-071 repeats it. A platform-level action with no tenant is recorded against
the account it was performed under; if a future action genuinely has none, that
is a plan-level question about what an untenanted action even is, not a nullable
column added quietly.

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

Why `TRUNCATE` is guarded too
-----------------------------
ADR-071 names `UPDATE` and `DELETE`, and row-level triggers do not fire on
`TRUNCATE` -- so a table guarded against only those two can still be emptied in
one statement by anyone who can reach it. That is the same erasure the ADR
forbids, reached by a different verb, and "indefinite and immutable" retention
(plan §12) does not survive it. A statement-level `BEFORE TRUNCATE` trigger
closes it, using the same function so the three refusals cannot drift apart.

The consequence is real and accepted: this table can never be emptied, including
by a test harness. Integration tests against it therefore run inside the
rolled-back `session` fixture rather than `committed_session`, and `audit_log`
is deliberately absent from the harness's TRUNCATE list.

Why `recorded_at` is separate from `occurred_at`
------------------------------------------------
`occurred_at` is when the action happened; `recorded_at` is when this row was
written. They differ whenever recording is deferred or replayed, and a log that
conflates them cannot answer "was this backdated?". ADR-006: both are timezone
aware, and nothing in this schema accepts a naive timestamp. The check
constraint re-asserts the domain's ordering invariant at the database, so a row
arriving by any other route -- a repair script, a bulk load -- is still refused.

Why there is no `version` column
--------------------------------
Every other table carries one for optimistic concurrency (ADR-057). This one
cannot use it: a version column exists to make `UPDATE ... WHERE version = ?`
detect a lost update, and there is no `UPDATE` here to protect. Adding one would
imply an update path that the triggers above make impossible.

Why the four indexes
--------------------
`account_id` because every tenant-scoped read filters on it and RLS will too;
`occurred_at` because the log is read as a time range far more often than by id;
`action` because "every CREDENTIAL_READ this week" is the question this table
exists to answer; `correlation_id` because joining an audit entry to the request
that caused it is the reason that column is carried at all. No composite index
yet -- adding one is cheap and guessing the leading column before there is a
query plan is not.
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
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        # The domain rejects a blank actor or subject; so does the table. A
        # record naming neither is a row saying "something happened", which is
        # not evidence of anything.
        #
        # The character set is given explicitly because bare `btrim(x)` strips
        # **spaces only** -- so a tab-only actor would satisfy it while the
        # domain's `.strip()` rejects the same value, and the constraint would
        # be weaker than the invariant it exists to mirror on exactly the route
        # it exists to cover. Python also strips `\v` and `\f`, which are not
        # included here; the domain remains the stricter of the two.
        sa.CheckConstraint(r"btrim(actor, E' \t\n\r') <> ''", name="ck_audit_log_actor"),
        sa.CheckConstraint(r"btrim(subject, E' \t\n\r') <> ''", name="ck_audit_log_subject"),
        sa.CheckConstraint("recorded_at >= occurred_at", name="ck_audit_log_ordering"),
    )
    op.create_index("ix_audit_log_account", "audit_log", ["account_id"])
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_correlation", "audit_log", ["correlation_id"])

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
    # Statement-level, because TRUNCATE has no rows to fire per-row triggers on.
    op.execute(
        f"CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {GUARD_FUNCTION}()"
    )


def downgrade() -> None:
    """Reverse the migration, removing the guard before the table it guards."""
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON audit_log")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_audit_log_correlation", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_index("ix_audit_log_account", table_name="audit_log")
    op.drop_table("audit_log")
