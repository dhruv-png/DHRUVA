"""What makes an audit record valid (plan §15.1, ADR-004, ADR-007, ADR-014).

Nothing here touches a database. The record is a value object, so every rule it
carries is assertable without one -- which is the point of deciding what a record
*is* before deciding how a table refuses to update it.

Two of these tests are pins rather than checks: the set of audited actions, and
the absence of a structured field. Both describe decisions someone made, and both
should fail loudly if a later change makes them silently untrue.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.domain.audit import AuditAction, AuditOutcome, AuditRecord
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
OCCURRED = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
CORRELATION = UUID("22222222-2222-2222-2222-222222222222")


def make_record(**overrides: object) -> AuditRecord:
    """Build a valid record, overriding one field at a time.

    A helper rather than a fixture because most tests here vary exactly one
    field, and naming that field at the call site is what makes the test
    readable.
    """
    fields: dict[str, object] = {
        "actor": "operator@dhruva.local",
        "action": AuditAction.AUTHENTICATION,
        "subject": "session",
        "outcome": AuditOutcome.SUCCEEDED,
        "occurred_at": OCCURRED,
        "recorded_at": OCCURRED,
        "account_id": ACCOUNT,
        "correlation_id": CORRELATION,
    }
    fields.update(overrides)
    return AuditRecord(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# What the plan requires be auditable
# --------------------------------------------------------------------------- #


def test_the_audited_actions_are_exactly_the_four_the_plan_names() -> None:
    """Pinned against plan §15.1, so a fifth cannot arrive unnoticed.

    The S06 design proposes a fifth -- reads of the credential store -- and this
    test is what makes accepting that ADR a visible act rather than an
    incidental commit.
    """
    assert sorted(action.value for action in AuditAction) == [
        "authentication",
        "configuration_change",
        "order_action",
        "risk_override",
    ]


def test_an_outcome_is_binary() -> None:
    """A partial success is two actions, not one outcome."""
    assert sorted(outcome.value for outcome in AuditOutcome) == ["failed", "succeeded"]


def test_a_failed_action_is_still_a_record() -> None:
    """Plan §15.1 says *every* authentication, and the failures are the signal.

    A log recording only successes cannot show a brute-force attempt, which is
    the single most likely thing anybody will read it for.
    """
    record = make_record(outcome=AuditOutcome.FAILED)

    assert record.outcome is AuditOutcome.FAILED


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_a_record_cannot_be_edited() -> None:
    """ADR-014: a record of the past that can be edited is not evidence.

    The database enforces this on the table. Enforcing it on the object too
    means a caller cannot even assemble a "corrected" record to hand to the
    store.
    """
    record = make_record()

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.outcome = AuditOutcome.FAILED  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# The anti-secret property, asserted structurally
# --------------------------------------------------------------------------- #

#: Field types an audit record may carry. Every one is a scalar a caller had to
#: name individually, which is what keeps a credential from arriving inside a
#: convenience field nobody reviewed.
PERMITTED_FIELD_TYPES = frozenset(
    {"str", "AuditAction", "AuditOutcome", "datetime", "AccountId", "UUID"}
)


def test_no_field_can_carry_arbitrary_structured_data() -> None:
    """The absence of a ``details`` mapping is a security property, not an omission.

    An audit log is written to be read widely, which makes it the worst place
    for a secret. A free-form field is where one would arrive -- a request body,
    an exception message, a token in a stack trace -- and ADR-037's redaction
    strategies cannot help a value that was deliberately stored.

    This fails if someone adds a ``Mapping``, a ``dict``, or an ``Any``. That is
    the intent: adding one should require deleting this test, in a diff a
    reviewer can see.
    """
    offenders = {
        field.name: field.type
        for field in dataclasses.fields(AuditRecord)
        if str(field.type) not in PERMITTED_FIELD_TYPES
    }

    assert not offenders, f"audit record gained a field that can hide a secret: {offenders}"


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("actor", ["", "   ", "\t"])
def test_a_record_must_name_an_actor(actor: str) -> None:
    """An audit entry without an actor answers "something happened" and nothing else."""
    with pytest.raises(InvariantViolation, match="must name an actor"):
        make_record(actor=actor)


@pytest.mark.parametrize("subject", ["", "   "])
def test_a_record_must_name_a_subject(subject: str) -> None:
    """Knowing that a configuration changed, without knowing which, is not an audit trail."""
    with pytest.raises(InvariantViolation, match="must name a subject"):
        make_record(subject=subject)


@pytest.mark.parametrize("field", ["occurred_at", "recorded_at"])
def test_both_timestamps_must_be_timezone_aware(field: str) -> None:
    """ADR-006 stores UTC. A naive datetime is one whose meaning depends on the host."""
    naive = datetime(2026, 8, 1, 9, 15)  # noqa: DTZ001 -- the defect under test

    with pytest.raises(InvariantViolation, match="must be timezone-aware"):
        make_record(**{field: naive})


def test_the_platform_cannot_record_an_action_before_it_happened() -> None:
    """A clock defect, caught where it is cheap rather than during an investigation.

    Audit ordering is only trustworthy if the timestamps are. A host with a
    skewed clock produces records that interleave wrongly, and the discovery
    normally happens while someone is trying to reconstruct an incident.
    """
    with pytest.raises(InvariantViolation, match="cannot precede"):
        make_record(recorded_at=OCCURRED - timedelta(seconds=1))


def test_recording_later_than_the_action_is_normal() -> None:
    """The usual case: a write retried, or a batch flushed after the fact (ADR-007)."""
    record = make_record(recorded_at=OCCURRED + timedelta(minutes=5))

    assert record.recorded_at > record.occurred_at


def test_a_record_carries_its_tenant_and_its_correlation() -> None:
    """ADR-004 for the tenant; S02's correlation id so the entry joins to its request."""
    record = make_record()

    assert record.account_id == ACCOUNT
    assert record.correlation_id == CORRELATION


def test_two_records_describing_the_same_event_are_equal() -> None:
    """Value semantics, so deduplicating or comparing records needs no identity column."""
    assert make_record() == make_record()
    assert make_record() != make_record(correlation_id=uuid4())
