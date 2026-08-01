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

from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
from dhruva.contexts.platform.domain.identity.credentials import Credential, EncryptedSecret
from dhruva.contexts.platform.infrastructure.persistence.records import (
    CredentialRecord,
    DailySnapshotRecord,
)
from dhruva.shared.identity import AccountId, CredentialId, InstrumentId
from dhruva.shared.money import Currency, Money, Price
from dhruva.shared.time import TradingDay

if TYPE_CHECKING:
    from uuid import UUID

    from dhruva.shared.time import TradingCalendar

__all__ = ["CredentialFactory", "DailySnapshotFactory"]


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
