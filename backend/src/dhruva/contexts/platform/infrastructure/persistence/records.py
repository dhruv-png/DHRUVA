"""Persistence records: the shape of a row, in primitives only.

A record sits between the SQLAlchemy model and the domain object. It carries no
domain types, so a mapper can produce one without needing a calendar, a currency
policy, or any other domain service.

Frozen and slotted because millions of these are created on a read path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

__all__ = ["CredentialRecord", "DailySnapshotRecord"]


@dataclass(frozen=True, slots=True)
class DailySnapshotRecord:
    """A ``daily_snapshot`` row, as primitives.

    Note ``trading_day`` is a plain :class:`datetime.date`. Lifting it to a
    ``TradingDay`` requires a calendar and happens in the factory.
    """

    id: UUID
    account_id: UUID
    instrument_id: UUID
    trading_day: date
    close_scaled_units: int
    turnover_minor_units: int
    currency: str
    version: int


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """A ``credential`` row, as primitives.

    ``ciphertext`` and ``wrapped_data_key`` are the two values ADR-070 says a row
    stores, and they are **bytes here rather than an**
    ``EncryptedSecret``. Pairing them is a domain invariant -- the two are only
    correct together -- and a record exists precisely to be the layer that has no
    invariants, so that a mapper can build one without importing the domain.

    No plaintext field exists, and none can: the record is what the mapper
    produces from a row, and a row holds no plaintext (ADR-070).

    ``id`` is the credential's domain identity here, not the anonymous surrogate
    the worked example uses. It is bound into the ciphertext's associated data,
    so it has to survive the round trip exactly.
    """

    id: UUID
    account_id: UUID
    broker: str
    wrapped_data_key: bytes
    ciphertext: bytes
    key_version: int
    rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
