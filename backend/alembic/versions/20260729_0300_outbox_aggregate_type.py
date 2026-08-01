"""Store the aggregate type on the outbox.

Revision ID: 0005_outbox_aggregate_type
Revises: 0004_outbox_lease
Created: 2026-07-29

Reversibility: reversible
Rollback procedure: `alembic downgrade 0004_outbox_lease` drops the column. The
    aggregate type of already-emitted events is then unrecoverable from this
    table, which is stated rather than assumed -- it cannot be re-derived, and
    that is precisely why the column exists.
Irreversible operations: none structurally; see the note above on data.
Expected runtime: sub-second. ADD COLUMN without NOT NULL does not rewrite the
    table on PostgreSQL 11+.
Operational impact: brief ACCESS EXCLUSIVE for the catalogue update. Safe during
    market hours.

Why this column rather than a derivation
----------------------------------------
ADR-061 puts `aggregate_type` on the envelope and ADR-062 makes it the routing
key: events are delivered per aggregate, and a stream is chosen by aggregate
type. The relay had no column to read it from, and the tempting fix was to derive
one from the event type's namespace.

That would be wrong in a way that is expensive and quiet. A derived type routes
events to a stream named after a guess, so two aggregates whose event types
happen to share a prefix would interleave on one stream while a consumer believed
it was reading one aggregate's ordered history. Per-aggregate ordering would still
*appear* to hold in every test that used a single aggregate.

A value that determines routing is a value the producer must state, not one the
infrastructure infers. Nullable during expand (ADR-055); the contract step making
it NOT NULL is TD-S05-7, alongside the three columns from migration 0003.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_outbox_aggregate_type"
down_revision: str | None = "0004_outbox_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the aggregate_type column."""
    op.add_column("outbox", sa.Column("aggregate_type", sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    op.drop_column("outbox", "aggregate_type")
