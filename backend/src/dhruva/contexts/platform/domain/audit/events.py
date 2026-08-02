"""``AuditRecorded`` -- the Platform context's one published event (plan §5).

Plan §5 gives C9 exactly one published event, and this is it. That scarcity is
deliberate: a context that publishes one fact has one contract to keep stable,
and every consumer of the platform's activity reads the same stream rather than
subscribing to a taxonomy that grows with every feature.

Why the event repeats the record rather than pointing at it
-----------------------------------------------------------
An ``AuditRecorded`` carrying only ``audit_id`` would force every consumer to
read the audit table to learn what happened -- which turns a notification into a
query, gives read access to the audit log to anybody who wanted a notification,
and breaks entirely for a consumer in another process that ADR-001 would rather
not couple to this schema. The fields are few and small, so carrying them is
cheaper than the join it avoids.

Why the identifiers are bare UUIDs
----------------------------------
:class:`~dhruva.shared.identity.AccountId` is a :class:`SurrogateId`, and ADR-061's
codecs have no entry for one. That absence is correct rather than an oversight:
every ``SurrogateId`` subclass shares a representation, so an encoded form could
be decoded back into an ``AccountId``, an ``InstrumentId`` or a ``CredentialId``
with nothing on the wire to say which. Encoding the identity and losing its type
would be worse than carrying a plain UUID, because it would *look* typed.

So the event carries the value, the audit table carries the value, and the
domain object -- which never crosses a transport -- keeps the type.

What it must not carry
----------------------
The same prohibition as the record itself (ADR-071, ADR-037): no credential, no
token, no value the redaction processors would strip from a log line. An event
travels further than a row does, so the rule is if anything stricter here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.shared.events import DomainEvent

if TYPE_CHECKING:
    from uuid import UUID

    from dhruva.contexts.platform.domain.audit.record import AuditAction, AuditOutcome

__all__ = ["AUDIT_AGGREGATE_TYPE", "AuditRecorded"]

#: Routing key for the outbox (ADR-062). Stated as a constant rather than derived
#: from a class name, because a stream is chosen by this value and a routing key
#: inferred from a naming convention sends events to a stream named after a guess.
AUDIT_AGGREGATE_TYPE = "audit_log"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditRecorded(DomainEvent):
    """An audit record was written.

    Past tense, and true by the time anybody sees it: the event is staged into
    the outbox in the same transaction as the row it describes (ADR-071), so a
    consumer that receives this can rely on the record existing. If the
    transaction rolls back, neither survives.

    Attributes
    ----------
    audit_id
        Identity of the row this describes, so a consumer that does have access
        to the audit log can find it without a second lookup key.
    actor
        Who acted. A string for the same reason the record's field is -- the
        platform has not yet decided whether it authenticates humans only or
        services too, and a string is the honest representation of that.
    action
        Which audited class this belongs to.
    subject
        What was acted upon.
    outcome
        Whether it took effect. Failures are published as well as successes: a
        run of failed authentications is a fact a consumer should be able to
        alert on, and it only exists if the failures were emitted.
    account_id
        Tenant scope, present from day one (ADR-004).
    correlation_id
        The identifier S02 binds to the request, so this event joins to the log
        lines, the outbox rows and the audit record of the same request.

    Notes
    -----
    ``occurred_at`` is inherited from :class:`DomainEvent` and is the audit
    record's ``occurred_at`` -- when the audited action happened, not when the
    row was written. The outbox stamps its own ``recorded_at`` from the injected
    clock (ADR-007, ADR-011), so both questions stay answerable.
    """

    audit_id: UUID
    actor: str
    action: AuditAction
    subject: str
    outcome: AuditOutcome
    account_id: UUID
    correlation_id: UUID
