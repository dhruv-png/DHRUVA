"""Primitive SQLAlchemy row models for the Reference context (ADR-052)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "CashInstrumentMappingRevisionModel",
    "FuturesContractModel",
    "FuturesContractRevisionModel",
    "InstrumentIdentityRevisionModel",
    "InstrumentMasterSnapshotModel",
    "InstrumentResolutionRevisionModel",
    "ReferenceBase",
    "ReferenceInstrumentModel",
    "WatchlistMembershipRevisionModel",
]


class ReferenceBase(DeclarativeBase):
    """Declarative metadata owned only by the Reference context."""


class ReferenceInstrumentModel(ReferenceBase):
    """Stable internal instrument identity; provider identifiers never key it."""

    __tablename__ = "reference_instrument"

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentIdentityRevisionModel(ReferenceBase):
    """An append-only point-in-time identity revision."""

    __tablename__ = "instrument_identity_revision"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "valid_from",
            "recorded_at",
            name="uq_instrument_identity_period_recorded",
        ),
        UniqueConstraint(
            "instrument_id",
            "source",
            "source_revision",
            name="uq_instrument_identity_source_revision",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_instrument_identity_validity",
        ),
        CheckConstraint(
            "(provider IS NULL AND instrument_token IS NULL AND exchange_token IS NULL) OR "
            "(provider IS NOT NULL AND instrument_token > 0 AND exchange_token > 0)",
            name="ck_instrument_identity_provider_mapping",
        ),
        CheckConstraint("btrim(canonical_symbol) <> ''", name="ck_instrument_identity_symbol"),
        CheckConstraint("btrim(company_name) <> ''", name="ck_instrument_identity_name"),
        CheckConstraint("btrim(sector) <> ''", name="ck_instrument_identity_sector"),
        CheckConstraint("btrim(source) <> ''", name="ck_instrument_identity_source"),
        CheckConstraint(
            "btrim(source_revision) <> ''",
            name="ck_instrument_identity_source_revision",
        ),
        Index(
            "ix_instrument_identity_as_of",
            "instrument_id",
            "valid_from",
            "recorded_at",
        ),
        Index("ix_instrument_identity_symbol", "canonical_symbol"),
        Index("ix_instrument_identity_isin", "isin"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reference_instrument.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text), nullable=False)
    former_names: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    sector: Mapped[str] = mapped_column(String(100), nullable=False)
    concentration_groups: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(Text),
        nullable=False,
    )
    cash_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    cash_trading_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    instrument_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exchange_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    futures_research_requested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)


class WatchlistMembershipRevisionModel(ReferenceBase):
    """An append-only revision of one tenant-shared watchlist membership."""

    __tablename__ = "watchlist_membership_revision"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "instrument_id",
            "active_from",
            "recorded_at",
            name="uq_watchlist_membership_period_recorded",
        ),
        UniqueConstraint(
            "account_id",
            "instrument_id",
            "source",
            "source_revision",
            name="uq_watchlist_membership_source_revision",
        ),
        CheckConstraint(
            "active_to IS NULL OR active_to >= active_from",
            name="ck_watchlist_membership_activity",
        ),
        CheckConstraint("btrim(source) <> ''", name="ck_watchlist_membership_source"),
        CheckConstraint(
            "btrim(source_revision) <> ''",
            name="ck_watchlist_membership_source_revision",
        ),
        Index(
            "ix_watchlist_membership_as_of",
            "account_id",
            "instrument_id",
            "active_from",
            "recorded_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reference_instrument.id", ondelete="CASCADE"),
        nullable=False,
    )
    active_from: Mapped[date] = mapped_column(Date, nullable=False)
    active_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)


class InstrumentMasterSnapshotModel(ReferenceBase):
    """Compressed, immutable evidence for one provider trading date."""

    __tablename__ = "instrument_master_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "market_date",
            name="uq_instrument_master_provider_date",
        ),
        CheckConstraint("btrim(provider) <> ''", name="ck_instrument_master_provider"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_instrument_master_sha256",
        ),
        CheckConstraint("compression = 'gzip'", name="ck_instrument_master_compression"),
        CheckConstraint("row_count > 0", name="ck_instrument_master_row_count"),
        CheckConstraint(
            "octet_length(compressed_csv) > 0",
            name="ck_instrument_master_payload",
        ),
        Index("ix_instrument_master_market_date", "market_date"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    market_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compression: Mapped[str] = mapped_column(String(8), nullable=False)
    compressed_csv: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)


class InstrumentResolutionRevisionModel(ReferenceBase):
    """Versioned availability decision for one underlying in one snapshot."""

    __tablename__ = "instrument_resolution_revision"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "instrument_id",
            "resolver_revision",
            name="uq_instrument_resolution_snapshot_instrument_resolver",
        ),
        CheckConstraint(
            "btrim(canonical_symbol) <> ''",
            name="ck_instrument_resolution_symbol",
        ),
        CheckConstraint(
            "btrim(resolver_revision) <> ''",
            name="ck_instrument_resolution_resolver",
        ),
        CheckConstraint(
            "(cash_available AND cash_unavailable_reason IS NULL) OR "
            "(NOT cash_available AND btrim(cash_unavailable_reason) <> '')",
            name="ck_instrument_resolution_cash_state",
        ),
        CheckConstraint(
            "futures_status IN ('AVAILABLE', 'CURRENTLY_UNAVAILABLE')",
            name="ck_instrument_resolution_futures_status",
        ),
        CheckConstraint(
            "btrim(futures_reason) <> ''",
            name="ck_instrument_resolution_futures_reason",
        ),
        CheckConstraint(
            "(futures_status = 'AVAILABLE' AND futures_contract_count > 0) OR "
            "(futures_status = 'CURRENTLY_UNAVAILABLE' AND futures_contract_count = 0)",
            name="ck_instrument_resolution_futures_count",
        ),
        Index(
            "ix_instrument_resolution_lookup",
            "snapshot_id",
            "resolver_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reference_instrument.id", ondelete="CASCADE"),
        nullable=False,
    )
    resolver_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    cash_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    cash_unavailable_reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    futures_status: Mapped[str] = mapped_column(String(32), nullable=False)
    futures_reason: Mapped[str] = mapped_column(String(240), nullable=False)
    futures_contract_count: Mapped[int] = mapped_column(Integer, nullable=False)


class CashInstrumentMappingRevisionModel(ReferenceBase):
    """Provider token mapping observed for one stable cash identity."""

    __tablename__ = "cash_instrument_mapping_revision"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "instrument_id",
            "resolver_revision",
            name="uq_cash_mapping_snapshot_instrument_resolver",
        ),
        CheckConstraint("btrim(provider) <> ''", name="ck_cash_mapping_provider"),
        CheckConstraint("btrim(exchange) <> ''", name="ck_cash_mapping_exchange"),
        CheckConstraint(
            "btrim(trading_symbol) <> ''",
            name="ck_cash_mapping_trading_symbol",
        ),
        CheckConstraint("instrument_token > 0", name="ck_cash_mapping_instrument_token"),
        CheckConstraint("exchange_token > 0", name="ck_cash_mapping_exchange_token"),
        Index(
            "ix_cash_mapping_instrument_snapshot",
            "instrument_id",
            "snapshot_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reference_instrument.id", ondelete="CASCADE"),
        nullable=False,
    )
    resolver_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exchange_token: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FuturesContractModel(ReferenceBase):
    """Stable actual-contract identity independent of rotating provider tokens."""

    __tablename__ = "futures_contract"
    __table_args__ = (
        UniqueConstraint(
            "underlying_id",
            "exchange",
            "expiry",
            name="uq_futures_contract_underlying_exchange_expiry",
        ),
        CheckConstraint("btrim(exchange) <> ''", name="ck_futures_contract_exchange"),
        Index("ix_futures_contract_underlying_expiry", "underlying_id", "expiry"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    underlying_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("reference_instrument.id", ondelete="CASCADE"),
        nullable=False,
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FuturesContractRevisionModel(ReferenceBase):
    """Provider metadata for one actual contract as observed in one daily master."""

    __tablename__ = "futures_contract_revision"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "contract_id",
            "resolver_revision",
            name="uq_futures_revision_snapshot_contract_resolver",
        ),
        CheckConstraint("btrim(provider) <> ''", name="ck_futures_revision_provider"),
        CheckConstraint("instrument_token > 0", name="ck_futures_revision_instrument_token"),
        CheckConstraint("exchange_token > 0", name="ck_futures_revision_exchange_token"),
        CheckConstraint(
            "btrim(trading_symbol) <> ''",
            name="ck_futures_revision_trading_symbol",
        ),
        CheckConstraint("lot_size > 0", name="ck_futures_revision_lot_size"),
        CheckConstraint("tick_size > 0", name="ck_futures_revision_tick_size"),
        CheckConstraint("instrument_type = 'FUT'", name="ck_futures_revision_type"),
        CheckConstraint("segment = 'NFO-FUT'", name="ck_futures_revision_segment"),
        CheckConstraint("exchange = 'NFO'", name="ck_futures_revision_exchange"),
        CheckConstraint(
            "contract_status IN ('ACTIVE', 'EXPIRED')",
            name="ck_futures_revision_status",
        ),
        CheckConstraint(
            "NOT selected_for_availability OR contract_status = 'ACTIVE'",
            name="ck_futures_revision_availability_selection",
        ),
        Index("ix_futures_revision_contract_snapshot", "contract_id", "snapshot_id"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    snapshot_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("instrument_master_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )
    contract_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("futures_contract.id", ondelete="CASCADE"),
        nullable=False,
    )
    resolver_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exchange_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trading_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False)
    segment: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    contract_status: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_for_availability: Mapped[bool] = mapped_column(Boolean, nullable=False)
