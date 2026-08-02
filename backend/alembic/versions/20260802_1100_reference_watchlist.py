"""Add effective-dated instruments and the shared watchlist.

Revision ID: 0013_reference_watchlist
Revises: 0012_rls_scaffolding
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0012_rls_scaffolding` removes the
    watchlist membership, identity revisions and stable instrument identities.
    Export owner configuration before rollback if rows beyond the committed
    owner fixture have been entered.
Irreversible operations: none
Expected runtime: sub-second on the initial empty tables.
Operational impact: three new empty tables and their indexes are created. No
    existing table is rewritten or locked. The account-owned membership table
    receives the same permissive v1 RLS scaffold as other tenant tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_reference_watchlist"
down_revision: str | None = "0012_rls_scaffolding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBERSHIP_POLICY = "watchlist_membership_revision_account_isolation"


def upgrade() -> None:
    """Create stable identities plus append-only bitemporal revisions."""
    op.create_table(
        "reference_instrument",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "instrument_identity_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("canonical_symbol", sa.String(length=32), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("former_names", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("sector", sa.String(length=100), nullable=False),
        sa.Column("concentration_groups", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("cash_exchange", sa.String(length=16), nullable=False),
        sa.Column("cash_trading_symbol", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("instrument_token", sa.BigInteger(), nullable=True),
        sa.Column("exchange_token", sa.BigInteger(), nullable=True),
        sa.Column("futures_research_requested", sa.Boolean(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.UniqueConstraint(
            "instrument_id",
            "valid_from",
            "recorded_at",
            name="uq_instrument_identity_period_recorded",
        ),
        sa.UniqueConstraint(
            "instrument_id",
            "source",
            "source_revision",
            name="uq_instrument_identity_source_revision",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_instrument_identity_validity",
        ),
        sa.CheckConstraint(
            "(provider IS NULL AND instrument_token IS NULL AND exchange_token IS NULL) OR "
            "(provider IS NOT NULL AND instrument_token > 0 AND exchange_token > 0)",
            name="ck_instrument_identity_provider_mapping",
        ),
        sa.CheckConstraint(
            "btrim(canonical_symbol) <> ''",
            name="ck_instrument_identity_symbol",
        ),
        sa.CheckConstraint("btrim(company_name) <> ''", name="ck_instrument_identity_name"),
        sa.CheckConstraint("btrim(sector) <> ''", name="ck_instrument_identity_sector"),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_instrument_identity_source"),
        sa.CheckConstraint(
            "btrim(source_revision) <> ''",
            name="ck_instrument_identity_source_revision",
        ),
    )
    op.create_index(
        "ix_instrument_identity_as_of",
        "instrument_identity_revision",
        ["instrument_id", "valid_from", "recorded_at"],
    )
    op.create_index(
        "ix_instrument_identity_symbol",
        "instrument_identity_revision",
        ["canonical_symbol"],
    )
    op.create_index(
        "ix_instrument_identity_isin",
        "instrument_identity_revision",
        ["isin"],
    )
    op.create_table(
        "watchlist_membership_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("active_from", sa.Date(), nullable=False),
        sa.Column("active_to", sa.Date(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_revision", sa.String(length=128), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "instrument_id",
            "active_from",
            "recorded_at",
            name="uq_watchlist_membership_period_recorded",
        ),
        sa.UniqueConstraint(
            "account_id",
            "instrument_id",
            "source",
            "source_revision",
            name="uq_watchlist_membership_source_revision",
        ),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to >= active_from",
            name="ck_watchlist_membership_activity",
        ),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_watchlist_membership_source"),
        sa.CheckConstraint(
            "btrim(source_revision) <> ''",
            name="ck_watchlist_membership_source_revision",
        ),
    )
    op.create_index(
        "ix_watchlist_membership_as_of",
        "watchlist_membership_revision",
        ["account_id", "instrument_id", "active_from", "recorded_at"],
    )
    op.execute("ALTER TABLE watchlist_membership_revision ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {MEMBERSHIP_POLICY} ON watchlist_membership_revision USING (true)")


def downgrade() -> None:
    """Remove the shared-watchlist schema in dependency order."""
    op.execute(f"DROP POLICY IF EXISTS {MEMBERSHIP_POLICY} ON watchlist_membership_revision")
    op.execute("ALTER TABLE watchlist_membership_revision DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_watchlist_membership_as_of", table_name="watchlist_membership_revision")
    op.drop_table("watchlist_membership_revision")
    op.drop_index("ix_instrument_identity_isin", table_name="instrument_identity_revision")
    op.drop_index("ix_instrument_identity_symbol", table_name="instrument_identity_revision")
    op.drop_index("ix_instrument_identity_as_of", table_name="instrument_identity_revision")
    op.drop_table("instrument_identity_revision")
    op.drop_table("reference_instrument")
