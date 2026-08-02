"""Primitive SQLAlchemy rows owned by the Market Data context."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["DailyMarketBarRevisionModel", "MarketDataBase"]


class MarketDataBase(DeclarativeBase):
    """Declarative metadata owned only by the Market Data context."""


class DailyMarketBarRevisionModel(MarketDataBase):
    """Append-only daily OHLCV revision with knowledge-time provenance."""

    __tablename__ = "daily_market_bar_revision"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trading_date",
            "source",
            "source_revision",
            name="uq_daily_bar_instrument_date_source_revision",
        ),
        CheckConstraint(
            "instrument_kind IN ('CASH_EQUITY', 'INDEX', 'FUTURES_CONTRACT')",
            name="ck_daily_bar_instrument_kind",
        ),
        CheckConstraint("open_price > 0", name="ck_daily_bar_open"),
        CheckConstraint("high_price > 0", name="ck_daily_bar_high"),
        CheckConstraint("low_price > 0", name="ck_daily_bar_low"),
        CheckConstraint("close_price > 0", name="ck_daily_bar_close"),
        CheckConstraint(
            "high_price >= open_price AND high_price >= close_price AND high_price >= low_price",
            name="ck_daily_bar_high_relationship",
        ),
        CheckConstraint(
            "low_price <= open_price AND low_price <= close_price AND low_price <= high_price",
            name="ck_daily_bar_low_relationship",
        ),
        CheckConstraint("volume >= 0", name="ck_daily_bar_volume"),
        CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="ck_daily_bar_open_interest",
        ),
        CheckConstraint("btrim(source) <> ''", name="ck_daily_bar_source"),
        CheckConstraint("source_instrument_id > 0", name="ck_daily_bar_source_instrument"),
        CheckConstraint(
            "adjustment_status IN ('RAW', 'ADJUSTED', 'VERIFIED', 'UNKNOWN')",
            name="ck_daily_bar_adjustment",
        ),
        CheckConstraint(
            "completeness IN ('COMPLETE', 'INCOMPLETE')",
            name="ck_daily_bar_completeness",
        ),
        CheckConstraint(
            "source_revision ~ '^[0-9a-f]{64}$'",
            name="ck_daily_bar_source_revision",
        ),
        CheckConstraint(
            "batch_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_daily_bar_batch_sha256",
        ),
        CheckConstraint(
            "quality_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_daily_bar_quality_revision",
        ),
        Index(
            "ix_daily_bar_point_in_time",
            "instrument_id",
            "trading_date",
            "retrieved_at",
        ),
        Index("ix_daily_bar_trading_date", "trading_date"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    instrument_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_instrument_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    adjustment_status: Mapped[str] = mapped_column(String(16), nullable=False)
    completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_revision: Mapped[str] = mapped_column(String(64), nullable=False)
