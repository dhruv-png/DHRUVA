"""The consumer idempotency ledger.

Revision ID: 0006_processed_event
Revises: 0005_outbox_aggregate_type
Created: 2026-07-31

Reversibility: reversible
Rollback procedure: `alembic downgrade 0005_outbox_aggregate_type` drops the
    table. Every consumer's record of what it has already processed goes with
    it, so the first delivery after a rollback is treated as new -- consumers
    would reprocess whatever the transport still holds. Stated rather than
    assumed: this is a rollback to perform with the consumers stopped.
Irreversible operations: none structurally; see the note above on data.
Expected runtime: sub-second. CREATE TABLE takes no lock on anything existing.
Operational impact: none. Nothing reads or writes this table until a consumer
    is deployed against it.

Why the primary key is a triple
-------------------------------
ADR-065. `(consumer_group, event_id, run_id)`, and each element earns its place.

`consumer_group`, because risk and reporting are separate consumers of the same
event and each must process it once. Keyed on `event_id` alone, whichever
consumer arrived first would silently suppress the other.

`event_id`, because that is the identity the producer stamped once and every
redelivery carries unchanged (ADR-061).

`run_id`, because of replay. Keyed only by `(consumer_group, event_id)`, a
backtest replaying the same history a second time would find every row already
present and process nothing -- reporting zero trades. A zero that looks like a
result is worse than an error, because nobody investigates it.

Why there is no unique index instead
------------------------------------
The primary key *is* the deduplication mechanism, not a guard on one. The
consumer inserts its ledger row inside the same transaction as its side effects,
so a duplicate raises a unique violation, the whole unit of work rolls back, and
the side effect does not happen twice. A check-then-insert would race; a
`ON CONFLICT DO NOTHING` would swallow the signal the consumer needs.

Why `processed_at` is indexed
-----------------------------
Retention. ADR-065 prunes rows after thirty days, and a scheduled delete over an
unindexed timestamp on a table that grows with event volume is a sequential scan
that gets slower every day it runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_processed_event"
down_revision: str | None = "0005_outbox_aggregate_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ledger table and its retention index."""
    op.create_table(
        "processed_event",
        sa.Column("consumer_group", sa.String(length=128), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("consumer_group", "event_id", "run_id", name="pk_processed_event"),
    )
    op.create_index("ix_processed_event_processed_at", "processed_event", ["processed_at"])
    # Replay prunes its own run on completion (ADR-065), which is a delete by
    # `run_id` alone. Without this it is a scan of every consumer's history to
    # find the rows of one backtest.
    op.create_index("ix_processed_event_run", "processed_event", ["run_id"])


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    op.drop_index("ix_processed_event_run", table_name="processed_event")
    op.drop_index("ix_processed_event_processed_at", table_name="processed_event")
    op.drop_table("processed_event")
