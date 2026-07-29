"""Initial schema: the worked example table and the transactional outbox.

Revision ID: 0001_initial
Revises:
Created: 2026-07-26

Reversibility: reversible
Rollback procedure: `alembic downgrade base` drops both tables. No data
    migration is involved, so the downgrade is complete -- it destroys the rows
    along with the tables, which is expected for an initial schema and is stated
    here rather than assumed.
Irreversible operations: none
Expected runtime: sub-second on any data volume; both tables are created empty.
Operational impact: no locks on existing objects, since nothing exists yet. Safe
    to run at any time, including during market hours.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the daily_snapshot and outbox tables."""
    op.create_table(
        "daily_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("close_scaled_units", sa.BigInteger(), nullable=False),
        sa.Column("turnover_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("account_id", "instrument_id", "trading_day", name="uq_daily_snapshot"),
        # Domain invariants re-asserted at the database, so a value arriving by
        # any route -- a manual UPDATE, another service, a bad migration -- is
        # still refused.
        sa.CheckConstraint("turnover_minor_units >= 0", name="ck_daily_snapshot_turnover"),
        sa.CheckConstraint("close_scaled_units <> 0", name="ck_daily_snapshot_close"),
        sa.CheckConstraint("version >= 1", name="ck_daily_snapshot_version"),
    )

    op.create_table(
        "outbox",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index("ix_outbox_unpublished", "outbox", ["published_at", "next_attempt_at"])
    op.create_index("ix_outbox_aggregate", "outbox", ["aggregate_id", "sequence"])


def downgrade() -> None:
    """Drop both tables, in dependency order."""
    op.drop_index("ix_outbox_aggregate", table_name="outbox")
    op.drop_index("ix_outbox_unpublished", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("daily_snapshot")
