"""The encrypted broker credential store.

Revision ID: 0007_credential
Revises: 0006_processed_event
Created: 2026-08-01

Reversibility: reversible
Rollback procedure: `alembic downgrade 0006_processed_event` drops the policy,
    then the table. Every stored broker credential goes with it, and because the
    plaintexts exist nowhere else (ADR-070), they are gone -- re-authenticating
    against each broker is the only recovery. This is a rollback to perform with
    the trading session stopped and the credentials re-enterable.
Irreversible operations: none structurally. The data loss above is a
    consequence of the drop, not of an unreversible DDL step.
Expected runtime: sub-second. CREATE TABLE takes no lock on anything existing,
    and the table is empty at creation.
Operational impact: none on deployment. Nothing reads or writes this table until
    the credential store is deployed against it. Enabling row-level security
    changes no behaviour today -- see the note below on why.

Why `(account_id, broker)` is unique rather than a named slot
-------------------------------------------------------------
One credential per broker per account, decided at Design Review. A named-slot
model -- several credentials per broker distinguished by a label -- is the
shape needed for key rotation with overlap, and for a second API key used by a
different strategy. Neither exists in v1, and the constraint is the cheaper
thing to relax later: dropping a unique constraint is a migration that cannot
lose data, whereas discovering that two rows silently collided is not.

Why `account_id` carries no foreign key
---------------------------------------
There is no `account` table in this database. `daily_snapshot` and `outbox`
already carry `account_id` as a bare UUID for the same reason, and this follows
that precedent rather than introducing an accounts table that no subsystem owns
yet. When accounts are modelled, one migration adds the constraint to all of
them together. Recorded because "FK" was the instruction and this is a
deliberate departure from it, not an oversight.

Why `key_version` exists before anything rotates
------------------------------------------------
ADR-070 makes master-key rotation a re-wrap of N data keys rather than a rewrite
of N ciphertexts, and TD-S06-4 defers the rotation job itself to S42. The job
needs to know which rows are still wrapped under the previous master key, and a
column added later would need a backfill over rows whose correct value nobody
can reconstruct. Cheap now, impossible later.

Why `version` exists, and why it is not `key_version`
-----------------------------------------------------
ADR-057: *every aggregate carries a `version` column*, and updates execute
`UPDATE ... WHERE id = :id AND version = :loaded_version` so that a lost update
raises rather than being overwritten. A credential is an aggregate -- it is
loaded, its secret is re-sealed on rotation, and it is stored again -- so the
rule applies to it.

The first draft of this migration omitted the column. That was a defect against
an accepted ADR rather than a deliberate exception: without it the repository's
`update()` cannot make the promise the `Repository` protocol publishes, and two
concurrent rotations would silently leave one credential wrapped under a key
nobody recorded. Corrected here rather than in a follow-up migration because
this one has not been committed, let alone released -- ADR-051 fixes release
tags, not unmerged revisions.

`key_version` is a different counter and the two are deliberately not merged.
`key_version` records *which master key* wrapped this row's data key, and S42's
rotation job is what moves it. `version` records *how many times this row has
been written*, and any writer moves it. A rotation bumps both; an ordinary
secret update bumps only `version`.

Why row-level security changes nothing yet
------------------------------------------
ADR-074: policies are authored for every table carrying `account_id`, and stay
**permissive in v1** exactly as ADR-004 says. The policy below admits every row.
It exists so that activation is a policy replacement rather than a schema
change, and so the restrictive form can be exercised against real PostgreSQL
before anyone depends on it.

Two things make this genuinely inert today. The policy predicate is `true`, and
the application connects as the table's owner -- and an owner bypasses RLS
unless the table is set to `FORCE ROW LEVEL SECURITY`, which it deliberately is
not. A test that proves the restrictive form bites must therefore both install
the restrictive predicate and force it, inside a transaction it rolls back.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_credential"
down_revision: str | None = "0006_processed_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The session variable the policies read, set by the Unit of Work because it
#: owns the transaction (ADR-053, ADR-074). Named here because the migration and
#: the Unit of Work must agree on it, and a mismatch would present as a policy
#: that silently admits nothing.
SESSION_ACCOUNT_SETTING = "dhruva.current_account_id"

POLICY_NAME = "credential_account_isolation"


def upgrade() -> None:
    """Create the credential table and its permissive account policy."""
    op.create_table(
        "credential",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker", sa.String(length=32), nullable=False),
        # ADR-070's two values, and only those two. The nonce is prefixed to each
        # blob rather than given a column, so that it cannot be separated from
        # the ciphertext it belongs to.
        sa.Column("wrapped_data_key", postgresql.BYTEA(), nullable=False),
        sa.Column("ciphertext", postgresql.BYTEA(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # ADR-057, and not the same thing as `key_version` above. `key_version`
        # says which master key wrapped this row's data key; `version` is the
        # optimistic-concurrency token every aggregate carries. They move for
        # different reasons and a rotation job bumps only the first.
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name="pk_credential"),
        sa.UniqueConstraint("account_id", "broker", name="uq_credential_account_broker"),
        sa.CheckConstraint("version >= 1", name="ck_credential_version"),
        sa.CheckConstraint("key_version >= 1", name="ck_credential_key_version"),
    )

    op.execute("ALTER TABLE credential ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY_NAME} ON credential USING (true)")


def downgrade() -> None:
    """Reverse the migration.

    Drops the policy explicitly before the table. `DROP TABLE` would remove it
    anyway, but an operator reading this should see the security object being
    removed rather than infer it.
    """
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON credential")
    op.execute("ALTER TABLE credential DISABLE ROW LEVEL SECURITY")
    op.drop_table("credential")
