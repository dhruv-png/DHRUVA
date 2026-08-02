"""Add point-in-time daily OHLCV revisions.

Revision ID: 0015_daily_market_bars
Revises: 0014_instrument_archive
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0014_instrument_archive` removes the
    daily bar revisions. Export any real history first if it has been collected.
Irreversible operations: none
Expected runtime: sub-second on the initial empty table.
Operational impact: one new empty global market-data table and two indexes are
    created. Existing tables are not rewritten or locked. This append-only daily
    table remains plain PostgreSQL; hypertable policies would add complexity
    without value at the approved 50-symbol end-of-day scale.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_daily_market_bars"
down_revision: str | None = "0014_instrument_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only point-in-time daily bar table."""
    op.create_table(
        "daily_market_bar_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_kind", sa.String(length=24), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("high_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("low_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("close_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("adjustment_status", sa.String(length=16), nullable=False),
        sa.Column("completeness", sa.String(length=16), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("batch_sha256", sa.String(length=64), nullable=False),
        sa.Column("quality_revision", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "instrument_id",
            "trading_date",
            "source",
            "source_revision",
            name="uq_daily_bar_instrument_date_source_revision",
        ),
        sa.CheckConstraint(
            "instrument_kind IN ('CASH_EQUITY', 'INDEX', 'FUTURES_CONTRACT')",
            name="ck_daily_bar_instrument_kind",
        ),
        sa.CheckConstraint("open_price > 0", name="ck_daily_bar_open"),
        sa.CheckConstraint("high_price > 0", name="ck_daily_bar_high"),
        sa.CheckConstraint("low_price > 0", name="ck_daily_bar_low"),
        sa.CheckConstraint("close_price > 0", name="ck_daily_bar_close"),
        sa.CheckConstraint(
            "high_price >= open_price AND high_price >= close_price AND high_price >= low_price",
            name="ck_daily_bar_high_relationship",
        ),
        sa.CheckConstraint(
            "low_price <= open_price AND low_price <= close_price AND low_price <= high_price",
            name="ck_daily_bar_low_relationship",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_daily_bar_volume"),
        sa.CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="ck_daily_bar_open_interest",
        ),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_daily_bar_source"),
        sa.CheckConstraint(
            "source_instrument_id > 0",
            name="ck_daily_bar_source_instrument",
        ),
        sa.CheckConstraint(
            "adjustment_status IN ('RAW', 'ADJUSTED', 'VERIFIED', 'UNKNOWN')",
            name="ck_daily_bar_adjustment",
        ),
        sa.CheckConstraint(
            "completeness IN ('COMPLETE', 'INCOMPLETE')",
            name="ck_daily_bar_completeness",
        ),
        sa.CheckConstraint(
            "source_revision ~ '^[0-9a-f]{64}$'",
            name="ck_daily_bar_source_revision",
        ),
        sa.CheckConstraint(
            "batch_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_daily_bar_batch_sha256",
        ),
        sa.CheckConstraint(
            "quality_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_daily_bar_quality_revision",
        ),
    )
    op.create_index(
        "ix_daily_bar_point_in_time",
        "daily_market_bar_revision",
        ["instrument_id", "trading_date", "retrieved_at"],
    )
    op.create_index(
        "ix_daily_bar_trading_date",
        "daily_market_bar_revision",
        ["trading_date"],
    )


def downgrade() -> None:
    """Remove only the daily market bar schema."""
    op.drop_index("ix_daily_bar_trading_date", table_name="daily_market_bar_revision")
    op.drop_index("ix_daily_bar_point_in_time", table_name="daily_market_bar_revision")
    op.drop_table("daily_market_bar_revision")
