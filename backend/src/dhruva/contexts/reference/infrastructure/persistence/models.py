"""Primitive SQLAlchemy row models for the Reference context (ADR-052)."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "InstrumentIdentityRevisionModel",
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
