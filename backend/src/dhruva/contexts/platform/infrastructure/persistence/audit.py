"""The append-only repository for the audit log (ADR-071, ADR-053, ADR-052).

Three properties, and the third is the one no other repository in the platform
has:

* **It never commits.** Transaction lifetime belongs to the Unit of Work
  (ADR-053). Nothing here calls ``commit``, ``rollback`` or ``begin`` -- which on
  this table is more than discipline, because the audit row must share a
  transaction with the action it records. A repository that committed would
  produce exactly the two failure modes ADR-071 rejects: an action with no record
  of it, and a record of an action that rolled back.
* **No SQLAlchemy type escapes.** Callers receive domain objects; models and
  records stay inside this layer (ADR-052).
* **There is no ``update`` and no ``delete``, and their absence is not the
  guarantee.** The database refuses both, and so does ``TRUNCATE``. That
  distinction is the whole of ADR-071: a repository that merely declines to offer
  the methods is one commit away from offering them, and its threat model
  includes the operator with a psql prompt -- the actor an application-level
  control cannot bind. The methods are absent here because there is nothing for
  them to call, not because their absence is doing the work.

Why this is a repository and not a writer
-----------------------------------------
It reads. An audit log nobody can query is a write-only file, and the questions
it exists to answer -- "every credential read this week", "everything that
happened under this correlation id" -- are reads. The read methods are
deliberately narrow and each corresponds to an index the migration creates; a
general query surface on this table would invite full scans over the one table
in the platform that grows without bound.

Why nothing here mints an identifier
------------------------------------
:meth:`AuditRepository.add` takes the row identity from its caller, like the
credential repository and unlike the worked example. The identity is published in
``AuditRecorded`` in the same transaction, so a consumer holds a reference to the
row; minting it privately here would mean the event could only be assembled after
the insert, splitting one atomic act into two ordered ones for no gain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_audit_log_model_kwargs,
    to_audit_log_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import AuditLogModel

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.platform.domain.audit import AuditRecord
    from dhruva.contexts.platform.infrastructure.persistence.factories import AuditFactory
    from dhruva.shared.identity import AccountId

__all__ = ["AuditRepository"]

#: Ceiling on any read that is not by primary key. The audit log is the one table
#: in the platform with no retention policy (plan §12), so an unbounded query
#: against it is a query whose cost grows forever -- and the caller that wrote it
#: will have tested it against an empty table.
DEFAULT_READ_LIMIT = 1000


class AuditRepository:
    """Appends and reads :class:`AuditRecord` values."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: AuditFactory) -> None:
        """Bind the repository to a session and a reconstruction factory."""
        self._session = session
        self._factory = factory

    async def add(self, record: AuditRecord, *, row_id: UUID) -> None:
        """Stage an audit record for insertion.

        Staged, not written: nothing reaches the database until the Unit of Work
        commits, which is what makes the audit row share one fate with the action
        it records (ADR-071).

        Parameters
        ----------
        record
            What happened. Already validated by its own invariants -- a blank
            actor or a ``recorded_at`` before ``occurred_at`` cannot be
            constructed, and the table's check constraints refuse them again for
            anything arriving by another route.
        row_id
            Identity for the row. Supplied rather than minted so the caller can
            publish the same value in ``AuditRecorded`` within this transaction.

        Notes
        -----
        Most callers should not use this directly. Use
        :class:`~dhruva.contexts.platform.infrastructure.audit.AuditRecorder`,
        which writes the row *and* stages the event, so neither can be done
        without the other.
        """
        flattened = self._factory.deconstruct(record, row_id)
        self._session.add(AuditLogModel(**to_audit_log_model_kwargs(flattened)))

    async def get(self, row_id: UUID) -> AuditRecord | None:
        """Return the record with this row identity, or ``None``.

        ``None`` rather than raising: a caller following a reference from an
        ``AuditRecorded`` event it received may legitimately be looking at a
        transaction that rolled back, and that is an answer rather than an error.
        """
        model = await self._session.get(AuditLogModel, row_id)
        if model is None:
            return None
        return self._factory.reconstruct(to_audit_log_record(model))

    async def for_correlation(
        self, correlation_id: UUID, *, limit: int = DEFAULT_READ_LIMIT
    ) -> tuple[AuditRecord, ...]:
        """Return everything audited under one correlation id, oldest first.

        The query the correlation column exists for, and the one asked during an
        incident: given a request, what did it actually do. Ordered by
        ``occurred_at`` rather than by insertion, because the question is about
        the sequence of actions rather than the sequence of writes.
        """
        statement = (
            select(AuditLogModel)
            .where(AuditLogModel.correlation_id == correlation_id)
            .order_by(AuditLogModel.occurred_at, AuditLogModel.id)
            .limit(limit)
        )
        return await self._reconstruct_all(statement)

    async def for_account(
        self, account_id: AccountId, *, limit: int = DEFAULT_READ_LIMIT
    ) -> tuple[AuditRecord, ...]:
        """Return an account's audit history, most recent first.

        Newest first here, oldest first in :meth:`for_correlation`, and the
        difference is not an inconsistency. A correlation is a finite sequence
        read forwards to follow what happened; an account's history is unbounded
        and read backwards from now.
        """
        statement = (
            select(AuditLogModel)
            .where(AuditLogModel.account_id == account_id.value)
            .order_by(AuditLogModel.occurred_at.desc(), AuditLogModel.id)
            .limit(limit)
        )
        return await self._reconstruct_all(statement)

    async def _reconstruct_all(
        self, statement: Select[tuple[AuditLogModel]]
    ) -> tuple[AuditRecord, ...]:
        """Execute a select of models and reconstruct every row it returns."""
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(self._factory.reconstruct(to_audit_log_record(model)) for model in models)
