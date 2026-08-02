"""The audit recorder: the one supported way to write an audit record.

ADR-071 requires two things of every audited action, and requires them together:

    The audit row is written **inside the transaction of the action it records**,
    and ``AuditRecorded`` is staged into the **outbox** in that same transaction.

Both halves are already available separately -- ``AuditRepository.add`` and
``UnitOfWork.add_event`` -- and that is exactly the problem this class exists to
solve. Two calls that must both happen is a rule; one call that does both is a
mechanism. ADR-071 closes by saying that every subsystem performing an audited
action inherits an obligation, which will be S23's order path, S06's own
authentication path, and every configuration change after that. An obligation
discharged by remembering two calls is one that will eventually be discharged by
remembering one.

So :meth:`AuditRecorder.record` writes the row and stages the event, and there is
no supported path that does one without the other.

Why this is infrastructure rather than application
--------------------------------------------------
It touches a repository and a transaction, which the application layer may not
import (ADR-059's layering, enforced by import-linter). What it does *not* do is
decide anything: it takes a fully-formed :class:`AuditRecord`, which the domain
already validated, and performs two writes. The judgement about what is worth
auditing belongs to the use case that calls this.

Why the event sink is a protocol
--------------------------------
``SqlAlchemyUnitOfWork`` satisfies it, and so does a fake with a list. Depending
on the concrete Unit of Work would make every test of this class need a database
to assert a rule that is about ordering and atomicity of two calls -- which is
unit-testable, and separately proven against real PostgreSQL where it belongs.

Why the recorder does not open its own transaction
--------------------------------------------------
It has no ``commit`` and takes no session factory. The transaction is the
caller's, because the audit row must share it with the action being audited
(ADR-053, ADR-071). A recorder that opened its own would produce a record of an
action that later rolled back -- which is worse than no record, because it is
evidence of something that did not happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from dhruva.contexts.platform.domain.audit import AUDIT_AGGREGATE_TYPE, AuditRecorded

if TYPE_CHECKING:
    from uuid import UUID

    from dhruva.contexts.platform.domain.audit import AuditRecord
    from dhruva.contexts.platform.infrastructure.persistence.audit import AuditRepository
    from dhruva.shared.events import DomainEvent

__all__ = ["AuditRecorder", "EventSink"]


class EventSink(Protocol):
    """Whatever stages a domain event inside the current transaction.

    Structurally satisfied by
    :class:`~dhruva.contexts.platform.infrastructure.database.unit_of_work.SqlAlchemyUnitOfWork`.
    Declared here rather than imported from there so this module depends on the
    one method it uses rather than on transaction management it does not touch.
    """

    def add_event(
        self,
        event: DomainEvent,
        *,
        aggregate_id: UUID | None = None,
        aggregate_type: str | None = None,
        account_id: UUID | None = None,
    ) -> None:
        """Stage an event into the outbox, inside this transaction."""
        ...


class AuditRecorder:
    """Writes an audit record and publishes the fact that it did.

    Examples
    --------
    ::

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            recorder = AuditRecorder(AuditRepository(uow.session, AuditFactory()), uow)
            await place_the_order(...)
            await recorder.record(audit_record)
            await uow.commit()

    The order within the block does not matter; that both are inside it does.
    """

    __slots__ = ("_events", "_repository")

    def __init__(self, repository: AuditRepository, events: EventSink) -> None:
        """Bind the recorder to the repository and the event sink of one transaction.

        Both must belong to the *same* Unit of Work. A recorder given a
        repository from one transaction and a sink from another would write the
        row and the event into two transactions that can fail independently,
        which is the failure this class exists to make unreachable -- so it is
        constructed per Unit of Work, alongside the repository, and never held
        across one.
        """
        self._repository = repository
        self._events = events

    async def record(self, record: AuditRecord) -> UUID:
        """Write the audit row and stage ``AuditRecorded``, in the caller's transaction.

        Returns
        -------
        UUID
            The row identity, which is also the ``audit_id`` carried by the
            event. Returned so a caller that wants to reference the record --
            in a response, or in a second audit record about the same request --
            need not query for it.

        Notes
        -----
        Nothing is committed and nothing is published here. The row and the
        outbox entry are both staged into the caller's transaction, so they
        commit together, roll back together, and the S05 relay delivers the
        event afterwards from durable state (AR-001b).

        The identity is minted here rather than taken as an argument. Unlike a
        credential, whose identity is bound into its ciphertext, an audit row's
        key is a surrogate with no meaning outside the table -- so there is
        nothing a caller could know about it that this does not, and requiring
        one would be asking every audited subsystem in the platform to invent a
        UUID correctly.
        """
        row_id = uuid4()
        await self._repository.add(record, row_id=row_id)
        self._events.add_event(
            AuditRecorded(
                occurred_at=record.occurred_at,
                audit_id=row_id,
                actor=record.actor,
                action=record.action,
                subject=record.subject,
                outcome=record.outcome,
                account_id=record.account_id.value,
                correlation_id=record.correlation_id,
            ),
            aggregate_id=row_id,
            aggregate_type=AUDIT_AGGREGATE_TYPE,
            account_id=record.account_id.value,
        )
        return row_id
