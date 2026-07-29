"""SQLAlchemy models. Persistence shape only.

These classes describe **rows**, not concepts. They carry no behaviour, no
invariants and no domain types, and nothing outside ``infrastructure`` ever sees
one (ADR-052).

The column types deliberately store primitives rather than domain objects here,
even though ``types.py`` provides `TypeDecorator`s that could do the conversion.
Keeping the model primitive means the mapper is the single place a conversion
happens, so a mapping regression has exactly one place to look -- and it keeps
the mapper independently benchmarkable, which ADR-036 requires.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "DailySnapshotModel"]


class Base(DeclarativeBase):
    """Declarative base for every model in the platform.

    One base, so Alembic's autogenerate sees a single metadata collection and
    cannot silently miss a table that registered against a different one.
    """


class DailySnapshotModel(Base):
    """The ``daily_snapshot`` table.

    Demonstrates every convention a real table must follow: surrogate UUID
    primary key, ``account_id`` on every row (ADR-004), a ``version`` column for
    optimistic concurrency (ADR-057), money stored as ``BIGINT`` minor units with
    its currency alongside (ADR-042), and constraints that re-assert domain
    invariants at the database so a value arriving by any other route is still
    refused.
    """

    __tablename__ = "daily_snapshot"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", "trading_day", name="uq_daily_snapshot"),
        CheckConstraint("turnover_minor_units >= 0", name="ck_daily_snapshot_turnover"),
        CheckConstraint("close_scaled_units <> 0", name="ck_daily_snapshot_close"),
        CheckConstraint("version >= 1", name="ck_daily_snapshot_version"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    close_scaled_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnover_minor_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # `default` is applied by the ORM on insert; `server_default` is DDL and is
    # what the migration actually created. Declaring only the first left the
    # model's metadata disagreeing with the live schema, so
    # `alembic revision --autogenerate` proposed dropping the server default --
    # meaning the next migration anyone generated for an unrelated change would
    # have silently carried an ALTER removing it in production. Both are
    # declared: the ORM path and any writer that bypasses it must agree.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
