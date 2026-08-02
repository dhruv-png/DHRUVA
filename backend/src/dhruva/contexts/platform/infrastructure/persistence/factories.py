"""Reconstruction factories: persistence record to domain object, and back.

This is the layer Amendment 2 added, and the reason it exists is
:class:`~dhruva.shared.time.TradingDay`. It cannot be constructed without a
calendar (ADR-046). A mapper holding a calendar would stop being a pure function;
a repository constructing one would be reaching for an infrastructure dependency
it should be given. A factory is the thing that legitimately owns domain services
and turns primitives into aggregates.

Deconstruction needs no services and is a pure function, so it lives here only
for symmetry -- keeping both directions in one module means a field added to the
aggregate is obviously missing from one side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.domain.audit import AuditAction, AuditOutcome, AuditRecord
from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.domain.identity.authorisation import (
    Permission,
    PermissionGrant,
    Role,
)
from dhruva.contexts.platform.domain.identity.credentials import Credential, EncryptedSecret
from dhruva.contexts.platform.domain.identity.passwords import PasswordHash
from dhruva.contexts.platform.domain.identity.principals import Principal
from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
from dhruva.contexts.platform.infrastructure.persistence.records import (
    AuditLogRecord,
    CredentialRecord,
    DailySnapshotRecord,
    PrincipalRecord,
    RefreshTokenRecord,
    RolePermissionRecord,
    RoleRecord,
)
from dhruva.shared.errors import DataQualityError
from dhruva.shared.identity import (
    AccountId,
    CredentialId,
    InstrumentId,
    PrincipalId,
    RefreshTokenId,
)
from dhruva.shared.money import Currency, Money, Price
from dhruva.shared.time import TradingDay

if TYPE_CHECKING:
    from uuid import UUID

    from dhruva.shared.time import TradingCalendar

__all__ = [
    "AuditFactory",
    "CredentialFactory",
    "DailySnapshotFactory",
    "PrincipalFactory",
    "RefreshTokenFactory",
    "RoleFactory",
]


class DailySnapshotFactory:
    """Reconstructs :class:`DailySnapshot` aggregates from persistence records.

    Holds the domain services reconstruction needs. Injected at the composition
    root and handed to the repository, so the repository orchestrates
    reconstruction without embedding it.
    """

    __slots__ = ("_calendar",)

    def __init__(self, calendar: TradingCalendar) -> None:
        """Bind the calendar used to verify trading days on read."""
        self._calendar = calendar

    def reconstruct(self, record: DailySnapshotRecord) -> DailySnapshot:
        """Build the aggregate, verifying its trading day against the calendar.

        Verification on read is deliberate. A stored date that is not a session --
        because a holiday was added retrospectively, or because a bad import
        wrote one -- must surface here rather than propagate into a backtest.
        This is what keeps ADR-046 uncompromised on the read path.
        """
        return DailySnapshot(
            instrument_id=InstrumentId(record.instrument_id),
            trading_day=TradingDay.of(record.trading_day, self._calendar),
            close=Price(record.close_scaled_units, Currency(record.currency)),
            turnover=Money(record.turnover_minor_units, Currency(record.currency)),
            account_id=AccountId(record.account_id),
            version=record.version,
        )

    @staticmethod
    def deconstruct(aggregate: DailySnapshot, row_id: UUID) -> DailySnapshotRecord:
        """Flatten the aggregate into a persistence record.

        ``row_id`` is supplied by the caller because a surrogate primary key is a
        persistence concern: the domain identifies a snapshot by instrument and
        trading day, and knows nothing about the row it lives in.
        """
        return DailySnapshotRecord(
            id=row_id,
            account_id=aggregate.account_id.value,
            instrument_id=aggregate.instrument_id.value,
            trading_day=aggregate.trading_day.on,
            close_scaled_units=aggregate.close.scaled_units,
            turnover_minor_units=aggregate.turnover.minor_units,
            currency=str(aggregate.turnover.currency),
            version=aggregate.version,
        )


class AuditFactory:
    """Reconstructs :class:`AuditRecord` value objects from persistence records.

    Holds no domain services, for the same reason :class:`CredentialFactory`
    holds none, and earns its place for the same two reasons.

    Reconstruction is not a copy. It lifts two bare UUIDs into
    :class:`~dhruva.shared.identity.AccountId`, lifts two strings into the domain
    enums, and re-runs the record's invariants -- so a row whose ``action`` is a
    value the enum has never heard of fails on read, here, rather than flowing
    into a compliance export as an unrecognised string nobody notices.

    That failure mode is worth naming, because on this table it is the plausible
    one. Nothing may update a row, so a bad value cannot arrive by an update; it
    arrives by an older or newer writer inserting one, and an audit log that
    silently returns values its own domain does not recognise is a log whose
    contents cannot be relied on.

    Stateless, so a single instance is safely shared.
    """

    __slots__ = ()

    @staticmethod
    def reconstruct(record: AuditLogRecord) -> AuditRecord:
        """Build the value object, re-asserting its invariants on the read path.

        Raises
        ------
        DataQualityError
            If ``action`` or ``outcome`` holds a value the domain enums do not
            define. A stored value outside the enum is bad *data*, not a bad
            argument from a caller, which is why this is not an
            :class:`~dhruva.shared.errors.InvariantViolation`.
        """
        try:
            action = AuditAction(record.action)
        except ValueError as error:
            raise _unknown_value("action", record, sorted(m.value for m in AuditAction)) from error
        try:
            outcome = AuditOutcome(record.outcome)
        except ValueError as error:
            raise _unknown_value(
                "outcome", record, sorted(m.value for m in AuditOutcome)
            ) from error

        return AuditRecord(
            actor=record.actor,
            action=action,
            subject=record.subject,
            outcome=outcome,
            occurred_at=record.occurred_at,
            recorded_at=record.recorded_at,
            account_id=AccountId(record.account_id),
            correlation_id=record.correlation_id,
        )

    @staticmethod
    def deconstruct(aggregate: AuditRecord, row_id: UUID) -> AuditLogRecord:
        """Flatten the value object into a persistence record.

        ``row_id`` is supplied by the caller, as it is for
        :meth:`DailySnapshotFactory.deconstruct` and for the same reason: the
        surrogate key is a persistence concern. A credential's key is different
        because its ciphertext is bound to it; nothing is bound to this one.

        The enums are written as their ``value``, never their ``name``. A
        ``StrEnum`` makes the two easy to confuse and only one of them is the
        stored contract -- ``str(AuditAction.ORDER_ACTION)`` is
        ``"order_action"``, while its name is ``"ORDER_ACTION"``, and a table
        holding a mixture of both is a table no query can filter.
        """
        return AuditLogRecord(
            id=row_id,
            account_id=aggregate.account_id.value,
            actor=aggregate.actor,
            action=aggregate.action.value,
            subject=aggregate.subject,
            outcome=aggregate.outcome.value,
            occurred_at=aggregate.occurred_at,
            recorded_at=aggregate.recorded_at,
            correlation_id=aggregate.correlation_id,
        )


def _unknown_value(field: str, record: AuditLogRecord, known: list[str]) -> DataQualityError:
    """Build the error for a stored value the domain enums do not define.

    Returned rather than raised, so the call site can chain it from the
    ``ValueError`` that revealed it -- which keeps the original in the traceback
    without this function having to know about it.

    The context names the row and the column, because ``AuditAction(stored)``
    alone raises a bare ``ValueError`` naming neither, and on a table with no
    update path an operator cannot fix the row. What they can do is find it, and
    that needs the identifier.
    """
    msg = f"audit_log row holds an unknown {field}"
    return DataQualityError(
        msg,
        row_id=str(record.id),
        field=field,
        stored=getattr(record, field),
        known=known,
    )


class PrincipalFactory:
    """Reconstructs :class:`Principal` aggregates from persistence records.

    The lift that matters here is ``password_hash``: a bare ``str`` in the record
    becomes a :class:`PasswordHash` on the aggregate, which is what makes a
    plaintext in that field unrepresentable everywhere above this line. The
    record keeps the primitive so the mapper can stay free of domain types
    (ADR-052); this is where the type comes back.

    Stateless, so a single instance is safely shared.
    """

    __slots__ = ()

    @staticmethod
    def reconstruct(record: PrincipalRecord) -> Principal:
        """Build the aggregate, re-asserting its invariants on the read path.

        The two TOTP columns are paired back into an ``EncryptedSecret`` or into
        ``None``. A row holding one without the other is refused by a check
        constraint, so reaching here with a half-pair would mean the constraint
        was dropped -- and pairing them defensively rather than asserting is
        wrong, because a secret with no wrapped key is one nothing can open and
        silently treating it as absent would report the principal as unenrolled.
        """
        sealed = None
        if record.totp_secret is not None and record.totp_wrapped_key is not None:
            sealed = EncryptedSecret(
                ciphertext=record.totp_secret,
                wrapped_data_key=record.totp_wrapped_key,
            )
        elif record.totp_secret is not None or record.totp_wrapped_key is not None:
            msg = "principal row holds half a sealed TOTP secret"
            raise DataQualityError(
                msg,
                row_id=str(record.id),
                has_ciphertext=record.totp_secret is not None,
                has_wrapped_key=record.totp_wrapped_key is not None,
            )

        return Principal(
            principal_id=PrincipalId(record.id),
            account_id=AccountId(record.account_id),
            subject=record.subject,
            password_hash=PasswordHash(record.password_hash),
            created_at=record.created_at,
            updated_at=record.updated_at,
            totp_secret=sealed,
            disabled_at=record.disabled_at,
            version=record.version,
        )

    @staticmethod
    def deconstruct(aggregate: Principal) -> PrincipalRecord:
        """Flatten the aggregate into a persistence record.

        No ``row_id`` argument. A principal's primary key is its domain identity,
        as a credential's is -- an audit record names an actor and a refresh
        token names its principal, and both references have to survive.
        """
        return PrincipalRecord(
            id=aggregate.principal_id.value,
            account_id=aggregate.account_id.value,
            subject=aggregate.subject,
            password_hash=aggregate.password_hash.encoded,
            totp_secret=aggregate.totp_secret.ciphertext if aggregate.totp_secret else None,
            totp_wrapped_key=(
                aggregate.totp_secret.wrapped_data_key if aggregate.totp_secret else None
            ),
            disabled_at=aggregate.disabled_at,
            created_at=aggregate.created_at,
            updated_at=aggregate.updated_at,
            version=aggregate.version,
        )


class RefreshTokenFactory:
    """Reconstructs :class:`RefreshToken` aggregates from persistence records.

    Stateless, so a single instance is safely shared.
    """

    __slots__ = ()

    @staticmethod
    def reconstruct(record: RefreshTokenRecord) -> RefreshToken:
        """Build the aggregate, re-asserting its invariants on the read path.

        The lineage-root invariant is re-checked here, which matters because it
        is what makes revocation complete: a stored row whose ``lineage_id`` had
        drifted would be revoked by nothing, and it would still authenticate.
        """
        return RefreshToken(
            token_id=RefreshTokenId(record.id),
            account_id=AccountId(record.account_id),
            principal_id=PrincipalId(record.principal_id),
            lineage_id=record.lineage_id,
            token_hash=record.token_hash,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            parent_token_id=(
                RefreshTokenId(record.parent_token_id) if record.parent_token_id else None
            ),
            replaced_by=RefreshTokenId(record.replaced_by) if record.replaced_by else None,
            revoked_at=record.revoked_at,
        )

    @staticmethod
    def deconstruct(aggregate: RefreshToken) -> RefreshTokenRecord:
        """Flatten the aggregate into a persistence record."""
        return RefreshTokenRecord(
            id=aggregate.token_id.value,
            account_id=aggregate.account_id.value,
            principal_id=aggregate.principal_id.value,
            lineage_id=aggregate.lineage_id,
            token_hash=aggregate.token_hash,
            parent_token_id=(
                aggregate.parent_token_id.value if aggregate.parent_token_id else None
            ),
            issued_at=aggregate.issued_at,
            expires_at=aggregate.expires_at,
            revoked_at=aggregate.revoked_at,
            replaced_by=aggregate.replaced_by.value if aggregate.replaced_by else None,
        )


class RoleFactory:
    """Reconstructs tenant-scoped roles and their accountable permission grants."""

    __slots__ = ()

    @staticmethod
    def reconstruct(
        record: RoleRecord,
        permission_records: tuple[RolePermissionRecord, ...],
    ) -> Role:
        """Build a role, refusing child rows for another role or unknown values."""
        grants: set[PermissionGrant] = set()
        for permission_record in permission_records:
            if (
                permission_record.account_id != record.account_id
                or permission_record.role_name != record.name
            ):
                msg = "role permission row belongs to another role"
                raise DataQualityError(
                    msg,
                    account_id=str(record.account_id),
                    role=record.name,
                    permission_account_id=str(permission_record.account_id),
                    permission_role=permission_record.role_name,
                )
            try:
                permission = Permission(permission_record.permission)
            except ValueError as error:
                msg = "role permission row holds an unknown permission"
                raise DataQualityError(
                    msg,
                    account_id=str(record.account_id),
                    role=record.name,
                    stored=permission_record.permission,
                    known=sorted(member.value for member in Permission),
                ) from error
            grants.add(
                PermissionGrant(
                    permission=permission,
                    granted_at=permission_record.granted_at,
                    granted_by=permission_record.granted_by,
                )
            )

        if len(grants) != len(permission_records):
            msg = "role permission rows contain a duplicate live grant"
            raise DataQualityError(msg, account_id=str(record.account_id), role=record.name)

        return Role(
            account_id=AccountId(record.account_id),
            name=record.name,
            created_at=record.created_at,
            updated_at=record.updated_at,
            grants=frozenset(grants),
            version=record.version,
        )

    @staticmethod
    def deconstruct(role: Role) -> tuple[RoleRecord, tuple[RolePermissionRecord, ...]]:
        """Flatten a role and its grant evidence into persistence records."""
        record = RoleRecord(
            account_id=role.account_id.value,
            name=role.name,
            created_at=role.created_at,
            updated_at=role.updated_at,
            version=role.version,
        )
        permission_records = tuple(
            RolePermissionRecord(
                account_id=role.account_id.value,
                role_name=role.name,
                permission=grant.permission.value,
                granted_at=grant.granted_at,
                granted_by=grant.granted_by,
            )
            for grant in sorted(role.grants, key=lambda item: item.permission.value)
        )
        return record, permission_records


class CredentialFactory:
    """Reconstructs :class:`Credential` aggregates from persistence records.

    Holds no domain services, and that is not an oversight worth collapsing the
    layer over. Two reasons it stays.

    The first is that reconstruction is not a copy. It lifts two bare UUIDs into
    :class:`~dhruva.shared.identity.CredentialId` and
    :class:`~dhruva.shared.identity.AccountId`, pairs two loose byte strings into
    an :class:`EncryptedSecret`, and runs the aggregate's invariants -- so a row
    that would produce an unopenable credential fails here, on read, rather than
    at the point some caller tries to authenticate against a broker with it. That
    is the same argument the worked example's calendar check makes.

    The second is that the mapper must stay pure to remain independently
    benchmarkable (ADR-036), and something has to own the domain types. Merging
    this into the mapper would put ``CredentialId`` in the module ADR-052 keeps
    free of domain types; merging it into the repository would put reconstruction
    in the module that is supposed to only orchestrate it.

    Stateless, so a single instance is safely shared. It is still injected rather
    than constructed inside the repository, matching how every other dependency
    reaches one.
    """

    __slots__ = ()

    @staticmethod
    def reconstruct(record: CredentialRecord) -> Credential:
        """Build the aggregate, re-asserting its invariants on the read path.

        No decryption happens here and none can: this class imports no cipher
        and takes no ``KeyProvider``. What comes back holds ciphertext, which is
        exactly what ADR-070 requires a read to return.
        """
        return Credential(
            credential_id=CredentialId(record.id),
            account_id=AccountId(record.account_id),
            broker=record.broker,
            secret=EncryptedSecret(
                ciphertext=record.ciphertext,
                wrapped_data_key=record.wrapped_data_key,
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
            key_version=record.key_version,
            rotated_at=record.rotated_at,
            version=record.version,
        )

    @staticmethod
    def deconstruct(aggregate: Credential) -> CredentialRecord:
        """Flatten the aggregate into a persistence record.

        No ``row_id`` argument, unlike :meth:`DailySnapshotFactory.deconstruct`.
        A credential's primary key is its domain identity rather than a surrogate
        the repository mints, because ADR-070's associated data binds the
        ciphertext to it -- an identifier invented at write time and forgotten
        could never be recomputed on read.
        """
        return CredentialRecord(
            id=aggregate.credential_id.value,
            account_id=aggregate.account_id.value,
            broker=aggregate.broker,
            wrapped_data_key=aggregate.secret.wrapped_data_key,
            ciphertext=aggregate.secret.ciphertext,
            key_version=aggregate.key_version,
            rotated_at=aggregate.rotated_at,
            created_at=aggregate.created_at,
            updated_at=aggregate.updated_at,
            version=aggregate.version,
        )
