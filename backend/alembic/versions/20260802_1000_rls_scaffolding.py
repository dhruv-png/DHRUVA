"""Complete the permissive row-level-security scaffolding.

Revision ID: 0012_rls_scaffolding
Revises: 0011_authorisation
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0011_authorisation` drops the
    `daily_snapshot` policy and disables row-level security on that table. The
    policy is permissive, so neither direction changes which rows deployed v1
    application connections can observe.
Irreversible operations: none
Expected runtime: sub-second. Both statements are catalogue-only operations.
Operational impact: `ALTER TABLE` takes an ACCESS EXCLUSIVE lock briefly while
    row-level security is enabled. No table rewrite or row lock occurs, and the
    policy predicate remains `true` in the deployed v1 posture.

Why only ``daily_snapshot`` changes here
----------------------------------------
The S06 migrations already enrolled every tenant-owned identity, credential,
audit and authorisation table as each was created. ``daily_snapshot`` predates
ADR-074 and is the one non-null ``account_id`` table that remained unenrolled.

``outbox.account_id`` is deliberately not included. It is nullable provenance
on transport infrastructure, and system events are valid without an account.
Treating every nullable account reference as tenant ownership would make those
events unrepresentable and would contradict the envelope contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_rls_scaffolding"
down_revision: str | None = "0011_authorisation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_NAME = "daily_snapshot_account_isolation"


def upgrade() -> None:
    """Enroll the remaining tenant-owned table with a permissive v1 policy."""
    op.execute("ALTER TABLE daily_snapshot ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY_NAME} ON daily_snapshot USING (true)")


def downgrade() -> None:
    """Remove exactly the scaffolding introduced by this revision."""
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON daily_snapshot")
    op.execute("ALTER TABLE daily_snapshot DISABLE ROW LEVEL SECURITY")
