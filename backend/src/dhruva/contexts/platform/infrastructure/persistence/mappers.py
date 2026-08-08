"""Mappers: SQLAlchemy model to persistence record, and back.

**Pure functions.** No services, no session, no IO, no domain types. That purity
is what makes them independently benchmarkable, which ADR-036 requires so a
future regression is attributable to the mapping layer rather than to the
database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.infrastructure.persistence.records import (
    AuditLogRecord,
    CredentialRecord,
    DailySnapshotRecord,
    PrincipalRecord,
    PrincipalRoleRecord,
    RefreshTokenRecord,
    RolePermissionRecord,
    RoleRecord,
)

if TYPE_CHECKING:
    from dhruva.contexts.platform.infrastructure.persistence.models import (
        AuditLogModel,
        CredentialModel,
        DailySnapshotModel,
        PrincipalModel,
        PrincipalRoleModel,
        RefreshTokenModel,
        RoleModel,
        RolePermissionModel,
    )

__all__ = [
    "to_audit_log_model_kwargs",
    "to_audit_log_record",
    "to_credential_model_kwargs",
    "to_credential_record",
    "to_model_kwargs",
    "to_principal_model_kwargs",
    "to_principal_record",
    "to_principal_role_model_kwargs",
    "to_principal_role_record",
    "to_record",
    "to_refresh_token_model_kwargs",
    "to_refresh_token_record",
    "to_role_model_kwargs",
    "to_role_permission_model_kwargs",
    "to_role_permission_record",
    "to_role_record",
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


def to_audit_log_record(model: AuditLogModel) -> AuditLogRecord:
    """Convert a loaded ``audit_log`` model into a persistence record.

    A read-only direction in practice. It exists because ADR-052 requires a
    round trip to be assertable -- ``to_domain(to_model(x)) == x`` -- and a
    mapping proven in only one direction is a mapping half tested.
    """
    return AuditLogRecord(
        id=model.id,
        account_id=model.account_id,
        actor=model.actor,
        action=model.action,
        subject=model.subject,
        outcome=model.outcome,
        occurred_at=model.occurred_at,
        recorded_at=model.recorded_at,
        correlation_id=model.correlation_id,
    )


def to_audit_log_model_kwargs(record: AuditLogRecord) -> dict[str, object]:
    """Convert an ``audit_log`` record into keyword arguments for the model.

    Kwargs rather than a constructed model, for consistency with the other two
    mappers -- though here the caller has only one choice. There is no update
    path to decide between: the database refuses one (ADR-071).
    """
    return {
        "id": record.id,
        "account_id": record.account_id,
        "actor": record.actor,
        "action": record.action,
        "subject": record.subject,
        "outcome": record.outcome,
        "occurred_at": record.occurred_at,
        "recorded_at": record.recorded_at,
        "correlation_id": record.correlation_id,
    }


def to_principal_record(model: PrincipalModel) -> PrincipalRecord:
    """Convert a loaded ``principal`` model into a persistence record."""
    return PrincipalRecord(
        id=model.id,
        account_id=model.account_id,
        subject=model.subject,
        password_hash=model.password_hash,
        totp_secret=model.totp_secret,
        totp_wrapped_key=model.totp_wrapped_key,
        disabled_at=model.disabled_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        version=model.version,
    )


def to_principal_model_kwargs(record: PrincipalRecord) -> dict[str, object]:
    """Convert a ``principal`` record into keyword arguments for the model."""
    return {
        "id": record.id,
        "account_id": record.account_id,
        "subject": record.subject,
        "password_hash": record.password_hash,
        "totp_secret": record.totp_secret,
        "totp_wrapped_key": record.totp_wrapped_key,
        "disabled_at": record.disabled_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "version": record.version,
    }


def to_refresh_token_record(model: RefreshTokenModel) -> RefreshTokenRecord:
    """Convert a loaded ``refresh_token`` model into a persistence record."""
    return RefreshTokenRecord(
        id=model.id,
        account_id=model.account_id,
        principal_id=model.principal_id,
        lineage_id=model.lineage_id,
        token_hash=model.token_hash,
        parent_token_id=model.parent_token_id,
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        revoked_at=model.revoked_at,
        replaced_by=model.replaced_by,
    )


def to_refresh_token_model_kwargs(record: RefreshTokenRecord) -> dict[str, object]:
    """Convert a ``refresh_token`` record into keyword arguments for the model."""
    return {
        "id": record.id,
        "account_id": record.account_id,
        "principal_id": record.principal_id,
        "lineage_id": record.lineage_id,
        "token_hash": record.token_hash,
        "parent_token_id": record.parent_token_id,
        "issued_at": record.issued_at,
        "expires_at": record.expires_at,
        "revoked_at": record.revoked_at,
        "replaced_by": record.replaced_by,
    }


def to_role_record(model: RoleModel) -> RoleRecord:
    """Convert a loaded ``role`` model into a persistence record."""
    return RoleRecord(
        account_id=model.account_id,
        name=model.name,
        created_at=model.created_at,
        updated_at=model.updated_at,
        version=model.version,
    )


def to_role_model_kwargs(record: RoleRecord) -> dict[str, object]:
    """Convert a ``role`` record into keyword arguments for the model."""
    return {
        "account_id": record.account_id,
        "name": record.name,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "version": record.version,
    }


def to_role_permission_record(model: RolePermissionModel) -> RolePermissionRecord:
    """Convert a loaded live permission grant into a persistence record."""
    return RolePermissionRecord(
        account_id=model.account_id,
        role_name=model.role_name,
        permission=model.permission,
        granted_at=model.granted_at,
        granted_by=model.granted_by,
    )


def to_role_permission_model_kwargs(record: RolePermissionRecord) -> dict[str, object]:
    """Convert a live permission grant into keyword arguments for the model."""
    return {
        "account_id": record.account_id,
        "role_name": record.role_name,
        "permission": record.permission,
        "granted_at": record.granted_at,
        "granted_by": record.granted_by,
    }


def to_principal_role_record(model: PrincipalRoleModel) -> PrincipalRoleRecord:
    """Convert a loaded principal assignment into a persistence record."""
    return PrincipalRoleRecord(
        principal_id=model.principal_id,
        account_id=model.account_id,
        role_name=model.role_name,
        assigned_at=model.assigned_at,
        assigned_by=model.assigned_by,
    )


def to_principal_role_model_kwargs(record: PrincipalRoleRecord) -> dict[str, object]:
    """Convert a principal assignment into keyword arguments for the model."""
    return {
        "principal_id": record.principal_id,
        "account_id": record.account_id,
        "role_name": record.role_name,
        "assigned_at": record.assigned_at,
        "assigned_by": record.assigned_by,
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
        purpose=model.purpose,
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
        "purpose": record.purpose,
        "wrapped_data_key": record.wrapped_data_key,
        "ciphertext": record.ciphertext,
        "key_version": record.key_version,
        "rotated_at": record.rotated_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "version": record.version,
    }
