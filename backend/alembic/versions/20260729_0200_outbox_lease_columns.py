"""Lease and dead-letter columns for the outbox relay.

Revision ID: 0004_outbox_lease
Revises: 0003_outbox_envelope
Created: 2026-07-29

Reversibility: reversible
Rollback procedure: `alembic downgrade 0003_outbox_envelope` drops the three
    columns and the partial index. Any lease held at that moment is forgotten,
    which is harmless: a lease is a short-lived reservation, not state anybody
    depends on. A row that was dead-lettered becomes claimable again, which is
    stated rather than assumed -- downgrade only where re-delivering
    dead-lettered events is acceptable.
Irreversible operations: none structurally; see the note above on dead letters.
Expected runtime: sub-second. ADD COLUMN without NOT NULL does not rewrite the
    table on PostgreSQL 11+, and the index is partial and on an empty predicate
    for existing rows.
Operational impact: brief ACCESS EXCLUSIVE for the catalogue update. Safe during
    market hours.

Why leases rather than held locks
---------------------------------
`FOR UPDATE SKIP LOCKED` stops two relays claiming one row, but holding that lock
across a publish would turn broker latency into lock contention -- a slow broker
would stall every other relay. So the claim transaction stamps `claimed_at` and
`claimed_by` and commits, reserving the row without anyone holding a database
lock while waiting on a network (ADR-063).

The trade is a new failure mode, and it is deliberate: a relay that dies
mid-publish leaves rows invisible until the lease expires. That is a latency
cliff rather than a loss, and the lease is short.

Why dead letters stay in this table
-----------------------------------
ADR-064 puts the dead-letter queue in PostgreSQL rather than Redis, because a
queue a FLUSHALL can erase is the worst possible home for exactly the events that
need a human. Keeping them here rather than in a second table means a
dead-lettered event keeps its identity, its attempt history and its last error in
one place, and `dead_lettered_at IS NOT NULL` is the whole query.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_outbox_lease"
down_revision: str | None = "0003_outbox_envelope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add lease and dead-letter columns, and the claim index."""
    op.add_column("outbox", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("claimed_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "outbox", sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Partial: the relay only ever scans undelivered rows, and a full index would
    # carry every event the platform has ever emitted for no benefit.
    op.create_index(
        "ix_outbox_claimable",
        "outbox",
        ["next_attempt_at", "claimed_at"],
        postgresql_where=sa.text("published_at IS NULL AND dead_lettered_at IS NULL"),
    )


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    op.drop_index("ix_outbox_claimable", table_name="outbox")
    op.drop_column("outbox", "dead_lettered_at")
    op.drop_column("outbox", "claimed_by")
    op.drop_column("outbox", "claimed_at")
