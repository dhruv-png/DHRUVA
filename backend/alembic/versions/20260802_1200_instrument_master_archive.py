"""Archive daily instrument masters and resolved provider mappings.

Revision ID: 0014_instrument_archive
Revises: 0013_reference_watchlist
Created: 2026-08-02

Reversibility: reversible
Rollback procedure: `alembic downgrade 0013_reference_watchlist` removes only
    archived provider snapshots, resolutions and contract mappings. Export any
    real provider archives first if they have been collected.
Irreversible operations: none
Expected runtime: sub-second on the initial empty tables.
Operational impact: five new empty global reference tables and their indexes are
    created. Existing tables are not rewritten. The archive is provider evidence,
    not tenant-owned state, so no account RLS policy is added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_instrument_archive"
down_revision: str | None = "0013_reference_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create immutable source evidence and versioned resolved mappings."""
    op.create_table(
        "instrument_master_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("market_date", sa.Date(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("compression", sa.String(length=8), nullable=False),
        sa.Column("compressed_csv", sa.LargeBinary(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "market_date",
            name="uq_instrument_master_provider_date",
        ),
        sa.CheckConstraint("btrim(provider) <> ''", name="ck_instrument_master_provider"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_instrument_master_sha256",
        ),
        sa.CheckConstraint(
            "compression = 'gzip'",
            name="ck_instrument_master_compression",
        ),
        sa.CheckConstraint("row_count > 0", name="ck_instrument_master_row_count"),
        sa.CheckConstraint(
            "octet_length(compressed_csv) > 0",
            name="ck_instrument_master_payload",
        ),
    )
    op.create_index(
        "ix_instrument_master_market_date",
        "instrument_master_snapshot",
        ["market_date"],
    )
    op.create_table(
        "instrument_resolution_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resolver_revision", sa.String(length=64), nullable=False),
        sa.Column("canonical_symbol", sa.String(length=64), nullable=False),
        sa.Column("cash_available", sa.Boolean(), nullable=False),
        sa.Column("cash_unavailable_reason", sa.String(length=240), nullable=True),
        sa.Column("futures_status", sa.String(length=32), nullable=False),
        sa.Column("futures_reason", sa.String(length=240), nullable=False),
        sa.Column("futures_contract_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "instrument_id",
            "resolver_revision",
            name="uq_instrument_resolution_snapshot_instrument_resolver",
        ),
        sa.CheckConstraint(
            "btrim(canonical_symbol) <> ''",
            name="ck_instrument_resolution_symbol",
        ),
        sa.CheckConstraint(
            "btrim(resolver_revision) <> ''",
            name="ck_instrument_resolution_resolver",
        ),
        sa.CheckConstraint(
            "(cash_available AND cash_unavailable_reason IS NULL) OR "
            "(NOT cash_available AND btrim(cash_unavailable_reason) <> '')",
            name="ck_instrument_resolution_cash_state",
        ),
        sa.CheckConstraint(
            "futures_status IN ('AVAILABLE', 'CURRENTLY_UNAVAILABLE')",
            name="ck_instrument_resolution_futures_status",
        ),
        sa.CheckConstraint(
            "btrim(futures_reason) <> ''",
            name="ck_instrument_resolution_futures_reason",
        ),
        sa.CheckConstraint(
            "(futures_status = 'AVAILABLE' AND futures_contract_count > 0) OR "
            "(futures_status = 'CURRENTLY_UNAVAILABLE' AND futures_contract_count = 0)",
            name="ck_instrument_resolution_futures_count",
        ),
    )
    op.create_index(
        "ix_instrument_resolution_lookup",
        "instrument_resolution_revision",
        ["snapshot_id", "resolver_revision"],
    )
    op.create_table(
        "cash_instrument_mapping_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resolver_revision", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("trading_symbol", sa.String(length=64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange_token", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "instrument_id",
            "resolver_revision",
            name="uq_cash_mapping_snapshot_instrument_resolver",
        ),
        sa.CheckConstraint("btrim(provider) <> ''", name="ck_cash_mapping_provider"),
        sa.CheckConstraint("btrim(exchange) <> ''", name="ck_cash_mapping_exchange"),
        sa.CheckConstraint(
            "btrim(trading_symbol) <> ''",
            name="ck_cash_mapping_trading_symbol",
        ),
        sa.CheckConstraint(
            "instrument_token > 0",
            name="ck_cash_mapping_instrument_token",
        ),
        sa.CheckConstraint("exchange_token > 0", name="ck_cash_mapping_exchange_token"),
    )
    op.create_index(
        "ix_cash_mapping_instrument_snapshot",
        "cash_instrument_mapping_revision",
        ["instrument_id", "snapshot_id"],
    )
    op.create_table(
        "futures_contract",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "underlying_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "underlying_id",
            "exchange",
            "expiry",
            name="uq_futures_contract_underlying_exchange_expiry",
        ),
        sa.CheckConstraint("btrim(exchange) <> ''", name="ck_futures_contract_exchange"),
    )
    op.create_index(
        "ix_futures_contract_underlying_expiry",
        "futures_contract",
        ["underlying_id", "expiry"],
    )
    op.create_table(
        "futures_contract_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("futures_contract.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resolver_revision", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("instrument_token", sa.BigInteger(), nullable=False),
        sa.Column("exchange_token", sa.BigInteger(), nullable=False),
        sa.Column("trading_symbol", sa.String(length=64), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("tick_size", sa.Numeric(18, 8), nullable=False),
        sa.Column("instrument_type", sa.String(length=16), nullable=False),
        sa.Column("segment", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("contract_status", sa.String(length=16), nullable=False),
        sa.Column("selected_for_availability", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "contract_id",
            "resolver_revision",
            name="uq_futures_revision_snapshot_contract_resolver",
        ),
        sa.CheckConstraint("btrim(provider) <> ''", name="ck_futures_revision_provider"),
        sa.CheckConstraint(
            "instrument_token > 0",
            name="ck_futures_revision_instrument_token",
        ),
        sa.CheckConstraint(
            "exchange_token > 0",
            name="ck_futures_revision_exchange_token",
        ),
        sa.CheckConstraint(
            "btrim(trading_symbol) <> ''",
            name="ck_futures_revision_trading_symbol",
        ),
        sa.CheckConstraint("lot_size > 0", name="ck_futures_revision_lot_size"),
        sa.CheckConstraint("tick_size > 0", name="ck_futures_revision_tick_size"),
        sa.CheckConstraint("instrument_type = 'FUT'", name="ck_futures_revision_type"),
        sa.CheckConstraint("segment = 'NFO-FUT'", name="ck_futures_revision_segment"),
        sa.CheckConstraint("exchange = 'NFO'", name="ck_futures_revision_exchange"),
        sa.CheckConstraint(
            "contract_status IN ('ACTIVE', 'EXPIRED')",
            name="ck_futures_revision_status",
        ),
        sa.CheckConstraint(
            "NOT selected_for_availability OR contract_status = 'ACTIVE'",
            name="ck_futures_revision_availability_selection",
        ),
    )
    op.create_index(
        "ix_futures_revision_contract_snapshot",
        "futures_contract_revision",
        ["contract_id", "snapshot_id"],
    )


def downgrade() -> None:
    """Remove only the provider archive schema in dependency order."""
    op.drop_index(
        "ix_futures_revision_contract_snapshot",
        table_name="futures_contract_revision",
    )
    op.drop_table("futures_contract_revision")
    op.drop_index(
        "ix_futures_contract_underlying_expiry",
        table_name="futures_contract",
    )
    op.drop_table("futures_contract")
    op.drop_index(
        "ix_cash_mapping_instrument_snapshot",
        table_name="cash_instrument_mapping_revision",
    )
    op.drop_table("cash_instrument_mapping_revision")
    op.drop_index(
        "ix_instrument_resolution_lookup",
        table_name="instrument_resolution_revision",
    )
    op.drop_table("instrument_resolution_revision")
    op.drop_index(
        "ix_instrument_master_market_date",
        table_name="instrument_master_snapshot",
    )
    op.drop_table("instrument_master_snapshot")
