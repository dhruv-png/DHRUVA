"""The recorder writes the row and the event together, or neither (ADR-071).

What these tests are for
------------------------
ADR-071 requires that an audit row and its ``AuditRecorded`` event share one
transaction. *That* they share one is proven against real PostgreSQL, in
``tests/integration/test_audit_log.py``, because only a database can be rolled
back and shown to have kept nothing.

What is provable here without one is the property that makes the integration
test's subject reachable at all: that a single call performs both writes, that
the two describe the same record, and that the identity the caller is handed is
the identity in both. A caller doing one of the two is the failure mode the
recorder exists to remove, and it is a failure of arithmetic rather than of
durability.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.domain.audit import (
    AUDIT_AGGREGATE_TYPE,
    AuditAction,
    AuditOutcome,
    AuditRecord,
    AuditRecorded,
)
from dhruva.contexts.platform.infrastructure.audit import AuditRecorder
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from dhruva.shared.events import DomainEvent

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
CORRELATION: Final = UUID("22222222-2222-2222-2222-222222222222")
OCCURRED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
RECORDED: Final = datetime(2026, 8, 1, 9, 16, tzinfo=UTC)


@dataclasses.dataclass(slots=True)
class StagedEvent:
    """One call to :meth:`FakeSink.add_event`, kept whole for assertion."""

    event: DomainEvent
    aggregate_id: UUID | None
    aggregate_type: str | None
    account_id: UUID | None


class FakeSink:
    """An event sink that records instead of staging.

    A fake rather than a mock: the assertions below are about what was staged,
    not about which methods were called, and a list makes that the obvious thing
    to write.
    """

    def __init__(self) -> None:
        self.staged: list[StagedEvent] = []

    def add_event(
        self,
        event: DomainEvent,
        *,
        aggregate_id: UUID | None = None,
        aggregate_type: str | None = None,
        account_id: UUID | None = None,
    ) -> None:
        """Record the staging call."""
        self.staged.append(StagedEvent(event, aggregate_id, aggregate_type, account_id))


class FakeRepository:
    """An audit repository that appends to a list.

    Deliberately offers no ``update`` and no ``delete``, matching the real one --
    a fake with methods the subject does not have is a fake that can pass a test
    the real object would fail.
    """

    def __init__(self) -> None:
        self.added: list[tuple[AuditRecord, UUID]] = []

    async def add(self, record: AuditRecord, *, row_id: UUID) -> None:
        """Record the insertion."""
        self.added.append((record, row_id))


class ExplodingRepository(FakeRepository):
    """A repository whose insert fails, to prove the event is not staged anyway."""

    async def add(
        self,
        record: AuditRecord,  # noqa: ARG002 - signature must match the real repository
        *,
        row_id: UUID,  # noqa: ARG002 - signature must match the real repository
    ) -> None:
        """Fail the way a constraint violation would."""
        msg = "the insert failed"
        raise RuntimeError(msg)


def make_record(**overrides: object) -> AuditRecord:
    """Build a valid audit record, overriding one field at a time."""
    fields: dict[str, object] = {
        "actor": "operator@dhruva.local",
        "action": AuditAction.CREDENTIAL_READ,
        "subject": "credential:zerodha",
        "outcome": AuditOutcome.SUCCEEDED,
        "occurred_at": OCCURRED,
        "recorded_at": RECORDED,
        "account_id": ACCOUNT,
        "correlation_id": CORRELATION,
    }
    fields.update(overrides)
    return AuditRecord(**fields)  # type: ignore[arg-type]


def make_recorder(
    repository: FakeRepository | None = None,
) -> tuple[AuditRecorder, FakeRepository, FakeSink]:
    """Assemble a recorder over fakes, returning all three for assertion."""
    store = repository or FakeRepository()
    sink = FakeSink()
    return AuditRecorder(store, sink), store, sink  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# One call, both writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_recording_writes_the_row_and_stages_the_event() -> None:
    """The whole reason this class exists rather than two calls at each call site."""
    recorder, store, sink = make_recorder()

    await recorder.record(make_record())

    assert len(store.added) == 1
    assert len(sink.staged) == 1


@pytest.mark.asyncio
async def test_the_row_and_the_event_carry_the_same_identity() -> None:
    """A consumer follows ``audit_id`` to the row, so the two must agree.

    They are assembled separately -- one goes through the repository, one into
    the outbox -- which is precisely why an identity that matched by convention
    rather than by construction would eventually stop matching.
    """
    recorder, store, sink = make_recorder()

    returned = await recorder.record(make_record())
    (_, row_id) = store.added[0]
    staged = sink.staged[0]

    assert isinstance(staged.event, AuditRecorded)
    assert returned == row_id == staged.event.audit_id


@pytest.mark.asyncio
async def test_the_event_describes_the_record_that_was_written() -> None:
    """Every field the event carries comes from the record, unchanged."""
    record = make_record(action=AuditAction.ORDER_ACTION, outcome=AuditOutcome.FAILED)
    recorder, _, sink = make_recorder()

    await recorder.record(record)
    event = sink.staged[0].event

    assert isinstance(event, AuditRecorded)
    assert event.actor == record.actor
    assert event.action is record.action
    assert event.subject == record.subject
    assert event.outcome is record.outcome
    assert event.account_id == record.account_id.value
    assert event.correlation_id == record.correlation_id


@pytest.mark.asyncio
async def test_the_event_occurred_when_the_action_did_not_when_it_was_recorded() -> None:
    """ADR-007, on the field a reader is most likely to misread.

    The record carries both instants; the event carries ``occurred_at``, because
    a fact's timestamp is when it became true. The outbox stamps its own
    ``recorded_at`` from an injected clock, so nothing is lost -- but conflating
    the two here would publish an event claiming an action happened at the
    moment it was written down, which for a replayed or deferred write is simply
    false.
    """
    record = make_record()
    recorder, _, sink = make_recorder()

    await recorder.record(record)

    assert sink.staged[0].event.occurred_at == OCCURRED
    assert record.recorded_at != OCCURRED


@pytest.mark.asyncio
async def test_the_event_is_routed_by_a_stated_aggregate_type() -> None:
    """ADR-062: the stream is chosen by this value, so it is stated not derived."""
    recorder, _, sink = make_recorder()

    await recorder.record(make_record())
    staged = sink.staged[0]

    assert staged.aggregate_type == AUDIT_AGGREGATE_TYPE
    assert staged.aggregate_id == staged.event.audit_id  # type: ignore[attr-defined]
    assert staged.account_id == ACCOUNT.value


@pytest.mark.asyncio
async def test_each_record_gets_its_own_identity() -> None:
    """Two identical actions are two records, not one recorded twice.

    A compensating record is the only way to correct this log, and it is by
    construction near-identical to the row it corrects. Sharing an identity
    between them would make the correction unreachable.
    """
    recorder, store, _ = make_recorder()

    first = await recorder.record(make_record())
    second = await recorder.record(make_record())

    assert first != second
    assert len(store.added) == 2


# --------------------------------------------------------------------------- #
# Neither, when the row cannot be written
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_failed_insert_stages_no_event() -> None:
    """An event announcing a record that does not exist is worse than silence.

    The transaction would roll back either way, so this is belt and braces --
    but the ordering it pins is the cheap half of the guarantee, and it holds
    without a database.
    """
    recorder, _, sink = make_recorder(ExplodingRepository())

    with pytest.raises(RuntimeError):
        await recorder.record(make_record())

    assert sink.staged == []


# --------------------------------------------------------------------------- #
# The anti-secret property, applied to the event
# --------------------------------------------------------------------------- #

#: Field types ``AuditRecorded`` may carry. Same reasoning as the record's
#: equivalent pin, and stricter in effect: an event travels further than a row.
PERMITTED_EVENT_FIELD_TYPES = frozenset(
    {
        "str",
        "AuditAction",
        "AuditOutcome",
        "UUID",
        "uuid.UUID",
        "datetime",
    }
)


def test_the_event_cannot_carry_arbitrary_structured_data() -> None:
    """A free-form field on the event would defeat the record's own prohibition.

    The record refuses one because an audit log is read widely. The event is
    read *further* -- it leaves the database, crosses a transport, and lands in
    consumers this context does not know about. A ``details`` mapping here would
    be the same defect with a longer blast radius (ADR-037, ADR-071).
    """
    offenders = {
        field.name: field.type
        for field in dataclasses.fields(AuditRecorded)
        if str(field.type) not in PERMITTED_EVENT_FIELD_TYPES
    }

    assert not offenders, f"AuditRecorded gained a field that can hide a secret: {offenders}"


def test_the_event_is_immutable() -> None:
    """A published fact that can be edited is not a fact."""
    event = AuditRecorded(
        occurred_at=OCCURRED,
        audit_id=uuid4(),
        actor="operator@dhruva.local",
        action=AuditAction.AUTHENTICATION,
        subject="session",
        outcome=AuditOutcome.SUCCEEDED,
        account_id=ACCOUNT.value,
        correlation_id=CORRELATION,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.outcome = AuditOutcome.FAILED  # type: ignore[misc]
