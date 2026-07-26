"""Mappers: SQLAlchemy model to persistence record, and back.

**Pure functions.** No services, no session, no IO, no domain types. That purity
is what makes them independently benchmarkable, which ADR-036 requires so a
future regression is attributable to the mapping layer rather than to the
database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.infrastructure.persistence.records import DailySnapshotRecord

if TYPE_CHECKING:
    from dhruva.contexts.platform.infrastructure.persistence.models import DailySnapshotModel

__all__ = ["to_model_kwargs", "to_record"]


def to_record(model: DailySnapshotModel) -> DailySnapshotRecord:
    """Convert a loaded model into a persistence record."""
    return DailySnapshotRecord(
        id=model.id,
        account_id=model.account_id,
        instrument_id=model.instrument_id,
        trading_day=model.trading_day,
        close_scaled_units=model.close_scaled_units,
        turnover_minor_units=model.turnover_minor_units,
        currency=model.currency,
        version=model.version,
    )


def to_model_kwargs(record: DailySnapshotRecord) -> dict[str, object]:
    """Convert a record into keyword arguments for the model.

    Returns kwargs rather than a constructed model so the caller decides whether
    it is inserting a new row or updating an existing one -- a distinction the
    mapper has no way to know and no business guessing.
    """
    return {
        "id": record.id,
        "account_id": record.account_id,
        "instrument_id": record.instrument_id,
        "trading_day": record.trading_day,
        "close_scaled_units": record.close_scaled_units,
        "turnover_minor_units": record.turnover_minor_units,
        "currency": record.currency,
        "version": record.version,
    }
