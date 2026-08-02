"""Refresh tokens, stored as hashes and rotated on every use.

Revision ID: 0009_refresh_token
Revises: 0008_audit_log
Created: 2026-08-01

Reversibility: reversible
Rollback procedure: `alembic downgrade 0008_audit_log` drops the policy and the
    table. Every outstanding refresh token becomes unrecognisable, so every
    session is effectively revoked and every operator re-authenticates. Access
    tokens already issued keep working until they expire (ADR-072 makes them
    stateless), so the window is bounded by their fifteen minutes.
Irreversible operations: none. The rows hold hashes, so there is nothing to
    preserve that could be restored anyway.
Expected runtime: sub-second. CREATE TABLE takes no lock on anything existing.
Operational impact: none on deployment. Nothing reads or writes this table until
    token issuance is deployed against it.

Why the column is `token_hash` and never the token
--------------------------------------------------
ADR-033 forbids storing a presented secret recoverably, and a refresh token is
exactly that -- a bearer credential whose whole value is that holding it proves
identity. A stolen database of plaintext refresh tokens is a stolen set of
sessions. The table therefore stores a hash and compares hashes, which is why
`token_hash` is `BYTEA` rather than text: it holds a digest, not an encoding of
one, and nothing should be tempted to print it.

Why rotation is a new row rather than an update
-----------------------------------------------
ADR-072: every successful refresh issues a new token and links the old row
through `replaced_by`. Updating the existing row in place would destroy the
evidence, and the evidence is the entire reuse-detection mechanism -- a token
that arrives already carrying `revoked_at` or `replaced_by` has been presented
twice, which means either a retry or a theft, and the system cannot tell which.
Treating it as theft is ADR-022's fail-closed posture applied to sessions.

`parent_token_id` and `replaced_by` are deliberately both present, pointing in
opposite directions along the same chain. `replaced_by` answers "was this one
superseded?", which is the question reuse detection asks of the token in hand.
`parent_token_id` answers "what came before this?", which is what a forensic
reader walks after the fact.

Why `lineage_id` exists as well
-------------------------------
Added when the token step was implemented, and the reason is worth stating
because the two link columns above look like they should already be enough.

Once reuse is detected, ADR-072 requires the **entire lineage** to be revoked.
Derived from `parent_token_id` alone that is a recursive walk: up the chain to
find the root, then down again to catch every descendant. That query is at its
slowest exactly when it matters most -- a long-lived session that has rotated
hundreds of times is both the most valuable to a thief and the most expensive to
walk -- and it has to be correct while a second request may be rotating the same
chain. A column every token in a family carries makes revocation one statement
with one predicate against one index, and makes it idempotent under concurrency.

The cost is one UUID per row and the obligation to carry it through rotation.
That is cheaper than a recursive CTE whose failure mode is a partially revoked
chain during an active compromise.

Why `principal_id` as well as `account_id`
------------------------------------------
`account_id` is the tenant (ADR-004); `principal_id` is **whose session this
is**. A refresh token naming only the account cannot answer "log this operator
out" in any deployment with two operators, and rotation would have no way to
check that the token being presented belongs to the principal it claims to.
Migration 0010 creates the table it refers to.

Why no foreign keys
-------------------
There is no `account` table, and `daily_snapshot`, `outbox`, `credential` and
`audit_log` all carry `account_id` as a bare UUID for that reason. When accounts
are modelled, one migration adds the constraint to all of them together.
`principal_id` is left unconstrained in the same migration for consistency with
that sweep rather than being the single column that gets a foreign key early.

Why `expires_at` is stored rather than derived
----------------------------------------------
The refresh lifetime is configuration, and configuration changes. A row that
derived its expiry from the current setting would silently extend or shorten
every outstanding session the moment that setting moved. Storing it fixes each
token's lifetime at the moment it was issued.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_refresh_token"
down_revision: str | None = "0008_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_NAME = "refresh_token_account_isolation"


def upgrade() -> None:
    """Create the refresh-token table, its lookup indexes and its policy."""
    op.create_table(
        "refresh_token",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lineage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("parent_token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_token"),
        # Presentation looks a token up by its hash and by nothing else, so this
        # is the access path. Unique because two rows sharing a hash would make
        # "which session is this?" unanswerable.
        sa.UniqueConstraint("token_hash", name="uq_refresh_token_hash"),
        # A token cannot stop being valid before it started being valid, and a
        # zero-length session is a configuration defect rather than a session.
        sa.CheckConstraint("expires_at > issued_at", name="ck_refresh_token_lifetime"),
        # The first token of a chain is its own lineage root. Enforcing that
        # here means `lineage_id` can never be an orphan pointing at nothing.
        sa.CheckConstraint(
            "(parent_token_id IS NOT NULL) OR (lineage_id = id)",
            name="ck_refresh_token_lineage_root",
        ),
    )
    # Lineage invalidation revokes a whole family in one statement once reuse is
    # detected; per-principal logout and expiry pruning are the other two reads.
    # All three are scans without these.
    op.create_index("ix_refresh_token_account", "refresh_token", ["account_id"])
    op.create_index("ix_refresh_token_principal", "refresh_token", ["principal_id"])
    op.create_index("ix_refresh_token_lineage", "refresh_token", ["lineage_id"])
    op.create_index("ix_refresh_token_parent", "refresh_token", ["parent_token_id"])
    op.create_index("ix_refresh_token_expires_at", "refresh_token", ["expires_at"])

    op.execute("ALTER TABLE refresh_token ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY_NAME} ON refresh_token USING (true)")


def downgrade() -> None:
    """Reverse the migration."""
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON refresh_token")
    op.execute("ALTER TABLE refresh_token DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_refresh_token_expires_at", table_name="refresh_token")
    op.drop_index("ix_refresh_token_parent", table_name="refresh_token")
    op.drop_index("ix_refresh_token_account", table_name="refresh_token")
    op.drop_table("refresh_token")
