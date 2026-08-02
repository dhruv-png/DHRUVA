"""What an audit record is (plan §15.1, ADR-004, ADR-007, ADR-014, ADR-037).

Pure domain. No database, no clock, no framework -- the whole module is a
function of its arguments, so what makes a record valid can be stated and tested
without either.

Why this exists before the table
--------------------------------
Plan §15.1 requires an append-only audit log for *every authentication,
configuration change, risk override, and order action*, and plan §12 fixes its
retention at indefinite and immutable. The **mechanism** that enforces
append-only in PostgreSQL is a decision belonging to the S06 design's proposed
ADR-071 and is not made here. What a record *is* does not depend on it.

Why there is no free-form payload field
---------------------------------------
Deliberate, and load-bearing. An audit log is written precisely so that it can be
read widely -- by an operator, by a reviewer, by a regulator -- which makes it the
worst possible place for a secret to land. A ``details: Mapping[str, Any]``
field would be the obvious place for one to arrive, and it would arrive by
accident: a caller passing a request body, a token in an error message, a
credential in a stack trace. ADR-037 gives the logging stack two independent
redaction strategies for exactly this reason, and neither can help a value that
was deliberately stored.

Every field below is a scalar the caller had to name individually, and
:func:`~tests.unit.audit.test_audit_record` asserts that this stays true. Adding
a structured field is therefore a deliberate, visible act rather than a
convenience nobody reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.shared.identity import AccountId
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

__all__ = ["UNATTRIBUTED_ACCOUNT", "AuditAction", "AuditOutcome", "AuditRecord"]

#: The account an audit record belongs to when it genuinely belongs to none.
#:
#: ADR-004 requires ``account_id NOT NULL`` on every domain table and ADR-071
#: repeats it for this one. Plan §15.1 separately requires that **every**
#: authentication be audited -- including one presenting a subject no principal
#: has, which by definition has no tenant. The two requirements meet here.
#:
#: A nullable column was the obvious escape and is the wrong one: it would make
#: "unattributed" and "nobody filled this in" the same representable state on the
#: one table that exists to be evidence, and it would leave a column RLS cannot
#: scope when S44 activates it.
#:
#: A reserved account keeps the column non-null, makes unattributed records a
#: thing a query can ask for by name, and costs one identifier. It is derived
#: rather than random so that it is the same value in every process and every
#: deployment -- an operator comparing two environments' audit exports is
#: comparing the same account.
#:
#: **Nothing may own this account.** No principal, credential or order may be
#: scoped to it: it is a label for records that belong to no tenant, and a real
#: row carrying it would make the label ambiguous.
UNATTRIBUTED_ACCOUNT: Final = AccountId.deterministic("audit", "unattributed")


class AuditAction(StrEnum):
    """The classes of action that must be audited.

    Four are enumerated by plan §15.1 and are not a judgement call. The fifth,
    :attr:`CREDENTIAL_READ`, was proposed by the S06 design and deferred here
    until the ADR deciding it was accepted -- because auditing a read is a
    different decision from auditing a write and costs differently. ADR-071 is
    now accepted and makes that call, so it is present.
    """

    AUTHENTICATION = "authentication"
    """A principal attempted to authenticate. Recorded whether it succeeded or
    failed: a run of failures is the signal, and it only exists if the failures
    were written down."""

    CONFIGURATION_CHANGE = "configuration_change"
    """Persisted, account-scoped configuration changed -- the Platform context's
    own territory (ADR-031). Process configuration is not audited here, because
    it is not stored here."""

    RISK_OVERRIDE = "risk_override"
    """A human overrode a risk decision. ADR-012 makes the risk gate
    unbypassable; an override is therefore a deliberate, recorded act rather
    than a code path."""

    ORDER_ACTION = "order_action"
    """An order was placed, modified or cancelled. Plan §15.2 requires an
    immutable order audit trail before G5, and this is the record that satisfies
    it."""

    CREDENTIAL_READ = "credential_read"
    """The credential store was read (ADR-071).

    The only audited *read* in the platform, and the exception is argued rather
    than assumed. Recording every read of every table would multiply write
    volume by read volume for a benefit nobody has asked for. This one is
    different in kind: "was this secret ever accessed, and by whom" is the
    question asked after a suspected compromise, and it is the one question that
    cannot be answered retrospectively -- an unrecorded read leaves nothing
    behind to find.

    The record names the credential; it never carries what the credential is.
    """


class AuditOutcome(StrEnum):
    """Whether the audited action took effect.

    Two values, not three. "Partially succeeded" is not an outcome an auditor can
    act on -- it is two actions that should have been recorded separately.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One thing that happened, and who caused it.

    Frozen, because a record of the past that can be edited is not evidence
    (ADR-014). The database enforces the same property on the table; this
    enforces it on the object, so a caller cannot assemble a "corrected" record
    and expect the store to take it.

    Attributes
    ----------
    actor
        Stable identifier of the principal that acted.

        Typed as ``str`` rather than as a domain identity **on purpose, and
        temporarily**. The S06 design records an open question -- whether the
        platform authenticates humans only or services too -- and the answer
        determines whether an actor is one type or a sum of two. A string is the
        honest representation of "not yet decided"; inventing a type here would
        make a decision look settled that is not.
    action
        Which of the four audited classes this belongs to.
    subject
        What was acted upon, identified stably enough to be found again. An
        instrument, an order, a configuration key, an account.
    outcome
        Whether it took effect.
    occurred_at
        When the action happened.
    recorded_at
        When the platform wrote it down (ADR-007). Distinct from ``occurred_at``
        because the two differ whenever a write is retried or replayed, and a
        reader needs to know which question they are asking.
    account_id
        The tenant this record belongs to. Present from day one (ADR-004), so no
        row needs retrofitting when multi-tenancy activates at S44.
    correlation_id
        The identifier S02 already binds to the request. Carried so that an audit
        entry joins to the log lines and events of the same request without
        anybody inventing a second identifier.
    """

    actor: str
    action: AuditAction
    subject: str
    outcome: AuditOutcome
    occurred_at: datetime
    recorded_at: datetime
    account_id: AccountId
    correlation_id: UUID

    def __post_init__(self) -> None:
        """Reject a record that could not identify what happened or when."""
        invariant(
            bool(self.actor.strip()),
            "an audit record must name an actor",
            action=str(self.action),
        )
        invariant(
            bool(self.subject.strip()),
            "an audit record must name a subject",
            action=str(self.action),
            actor=self.actor,
        )
        for name in ("occurred_at", "recorded_at"):
            value: datetime = getattr(self, name)
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"{name} must be timezone-aware",
                field=name,
                value=value.isoformat(),
            )
        # The platform cannot learn of an action before it happens. When this
        # fires it is a clock defect -- a skewed host, or a caller passing a
        # naive local time it converted wrongly -- and an audit log built on a
        # broken clock is one whose ordering cannot be trusted.
        invariant(
            self.recorded_at >= self.occurred_at,
            "recorded_at cannot precede occurred_at",
            occurred_at=self.occurred_at.isoformat(),
            recorded_at=self.recorded_at.isoformat(),
        )
