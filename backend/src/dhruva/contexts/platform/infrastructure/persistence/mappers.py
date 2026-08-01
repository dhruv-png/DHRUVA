"""Mappers: SQLAlchemy model to persistence record, and back.

**Pure functions.** No services, no session, no IO, no domain types. That purity
is what makes them independently benchmarkable, which ADR-036 requires so a
future regression is attributable to the mapping layer rather than to the
database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.infrastructure.persistence.records import (
    CredentialRecord,
    DailySnapshotRecord,
)

if TYPE_CHECKING:
    from dhruva.contexts.platform.infrastructure.persistence.models import (
        CredentialModel,
        DailySnapshotModel,
    )

__all__ = [
    "to_credential_model_kwargs",
    "to_credential_record",
    "to_model_kwargs",
    "to_record",
]


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


def to_credential_record(model: CredentialModel) -> CredentialRecord:
    """Convert a loaded ``credential`` model into a persistence record.

    Copies ciphertext and wrapped key across unchanged and unexamined. This
    function cannot tell whether the bytes it is moving are intact -- only a
    decryption can -- and that is the correct division: authentication belongs
    to the cipher, not to the mapping layer.
    """
    return CredentialRecord(
        id=model.id,
        account_id=model.account_id,
        broker=model.broker,
        wrapped_data_key=model.wrapped_data_key,
        ciphertext=model.ciphertext,
        key_version=model.key_version,
        rotated_at=model.rotated_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        version=model.version,
    )


def to_credential_model_kwargs(record: CredentialRecord) -> dict[str, object]:
    """Convert a ``credential`` record into keyword arguments for the model.

    Kwargs rather than a constructed model, for the same reason as
    :func:`to_model_kwargs`: whether this is an insert or an update is the
    caller's knowledge, not the mapper's.
    """
    return {
        "id": record.id,
        "account_id": record.account_id,
        "broker": record.broker,
        "wrapped_data_key": record.wrapped_data_key,
        "ciphertext": record.ciphertext,
        "key_version": record.key_version,
        "rotated_at": record.rotated_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "version": record.version,
    }
