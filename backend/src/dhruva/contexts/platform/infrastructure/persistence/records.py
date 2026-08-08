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

__all__ = [
    "AuditLogRecord",
    "CredentialRecord",
    "DailySnapshotRecord",
    "PrincipalRecord",
    "PrincipalRoleRecord",
    "RefreshTokenRecord",
    "RolePermissionRecord",
    "RoleRecord",
]


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
class AuditLogRecord:
    """An ``audit_log`` row, as primitives.

    Named ``AuditLogRecord`` rather than ``AuditRecord`` because the domain
    already owns that name for the value object this flattens. Two types called
    the same thing on either side of a mapping layer is how an import of the
    wrong one survives review.

    ``action`` and ``outcome`` are plain strings here, not the domain enums. That
    is the layer doing its job: a record exists to carry primitives so the mapper
    can build one without importing the domain, and lifting a string back into an
    :class:`~dhruva.contexts.platform.domain.audit.AuditAction` -- which fails
    loudly on a value the enum does not know -- is the factory's work.

    There is no ``version`` field, because the table has no version column and
    nothing may update this row (ADR-071).
    """

    id: UUID
    account_id: UUID
    actor: str
    action: str
    subject: str
    outcome: str
    occurred_at: datetime
    recorded_at: datetime
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    """A ``principal`` row, as primitives.

    ``password_hash`` is a bare ``str`` here rather than a
    :class:`~...domain.identity.PasswordHash`, and that is the layer doing its
    job rather than a weakening of the type. A record exists to carry primitives
    so that a mapper can build one without importing the domain; the factory is
    what lifts the string back into the type that makes a plaintext in this field
    unrepresentable.

    ``totp_secret`` and ``totp_wrapped_key`` are separate optional fields for the
    same reason :class:`CredentialRecord` splits its two: pairing them is a
    domain invariant, and a record is the layer that has none.
    """

    id: UUID
    account_id: UUID
    subject: str
    password_hash: str
    totp_secret: bytes | None
    totp_wrapped_key: bytes | None
    disabled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    """A ``refresh_token`` row, as primitives.

    No ``version`` field, matching the table. Rotation and revocation are
    conditional updates that carry their own concurrency control in the
    ``WHERE`` clause, so there is no lost update for a version column to detect.
    """

    id: UUID
    account_id: UUID
    principal_id: UUID
    lineage_id: UUID
    token_hash: bytes
    parent_token_id: UUID | None
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by: UUID | None


@dataclass(frozen=True, slots=True)
class RoleRecord:
    """A tenant-scoped ``role`` row, excluding its child grant rows."""

    account_id: UUID
    name: str
    created_at: datetime
    updated_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class RolePermissionRecord:
    """One live ``role_permission`` row, including its accountable actor."""

    account_id: UUID
    role_name: str
    permission: str
    granted_at: datetime
    granted_by: str


@dataclass(frozen=True, slots=True)
class PrincipalRoleRecord:
    """The single role currently assigned to a principal."""

    principal_id: UUID
    account_id: UUID
    role_name: str
    assigned_at: datetime
    assigned_by: str


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
    so it has to survive the round trip exactly. ``purpose`` is bound there too
    (ADR-077) and is a plain string at this layer for the same reason every
    other field is: a record has no invariants, and converting it to the closed
    enum is the factory's job.
    """

    id: UUID
    account_id: UUID
    broker: str
    purpose: str
    wrapped_data_key: bytes
    ciphertext: bytes
    key_version: int
    rotated_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
