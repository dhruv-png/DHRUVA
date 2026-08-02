"""The audit log against real PostgreSQL (ADR-058, ADR-071, ADR-053).

This is the file ADR-071 is actually decided by. Everything in it is a claim
about what the *database* refuses, and none of it is checkable anywhere else:

    Append-only means the database refuses, not that the code declines. A table
    whose immutability is a convention is one ``UPDATE`` away from being
    worthless as evidence.

A fake session cannot refuse an ``UPDATE``. A repository that offers no update
method proves only that this repository offers no update method -- and the threat
model ADR-071 names includes the operator with a psql prompt, who is not calling
the repository at all. So each of the three refusals below is executed as raw SQL
against the real table, deliberately bypassing every application-level control,
because that is the only form of the assertion worth making.

Why nothing here commits
------------------------
``audit_log`` is guarded against ``TRUNCATE`` as well, so nothing can empty it --
which is the intended property and which means a test that genuinely committed
would leave a row behind permanently, in every developer's database, once per
run. So the schema tests use the rolled-back ``session`` fixture, and the two
tests that need a real Unit of Work assert *inside* the open transaction and then
fail it deliberately.

That is not a weaker test. The property under examination is that the audit row
and its outbox event share one fate, and observing them both present and then
both gone is the same guarantee seen from the other side -- with the advantage
that a suite proving immutability does not also have to leave immutable rows
lying around.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.platform.domain.audit import (
    AUDIT_AGGREGATE_TYPE,
    AuditAction,
    AuditOutcome,
    AuditRecord,
)
from dhruva.contexts.platform.infrastructure.audit import AuditRecorder
from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.persistence.audit import AuditRepository
from dhruva.contexts.platform.infrastructure.persistence.factories import AuditFactory
from dhruva.shared.identity import AccountId
from dhruva.shared.messaging import decode_value
from dhruva.shared.time import FrozenClock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from dhruva.shared.events import DomainEvent

# See `test_repository_correctness` for why the markers are declared here rather
# than synthesised at collection time.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

FACTORY: Final = AuditFactory()
ACCOUNT: Final = AccountId.deterministic("primary")
OTHER_ACCOUNT: Final = AccountId.deterministic("secondary")
OCCURRED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
RECORDED: Final = datetime(2026, 8, 1, 9, 20, tzinfo=UTC)

#: The SQLSTATE the append-only guard raises with. Asserted on rather than the
#: message text, so a reworded message does not silently stop being checked --
#: and so a caller could match on it too.
RESTRICT_VIOLATION: Final = "23001"


class RecordingSink:
    """Stands in for the Unit of Work's event staging.

    Used only by the test that asks *what* was staged and how it was routed --
    a question about the recorder's arguments, which a real outbox row would
    answer less directly. The tests that ask whether the two writes actually
    share a transaction use the real
    :class:`~...database.unit_of_work.SqlAlchemyUnitOfWork` and the real outbox
    table, because a fake cannot be rolled back.
    """

    def __init__(self) -> None:
        self.staged: list[tuple[DomainEvent, UUID | None, str | None, UUID | None]] = []

    def add_event(
        self,
        event: DomainEvent,
        *,
        aggregate_id: UUID | None = None,
        aggregate_type: str | None = None,
        account_id: UUID | None = None,
    ) -> None:
        """Record the staging call."""
        self.staged.append((event, aggregate_id, aggregate_type, account_id))


def _record(**overrides: object) -> AuditRecord:
    """Build a valid audit record, overriding one field at a time."""
    defaults: dict[str, object] = {
        "actor": "operator@dhruva.local",
        "action": AuditAction.CREDENTIAL_READ,
        "subject": "credential:zerodha",
        "outcome": AuditOutcome.SUCCEEDED,
        "occurred_at": OCCURRED,
        "recorded_at": OCCURRED,
        "account_id": ACCOUNT,
        "correlation_id": uuid4(),
    }
    return AuditRecord(**{**defaults, **overrides})  # type: ignore[arg-type]


def _uow(engine: AsyncEngine) -> SqlAlchemyUnitOfWork:
    """Build a real Unit of Work over the migrated engine.

    A frozen clock, so the outbox row's ``recorded_at`` is a known value rather
    than whatever the wall clock said (ADR-011).
    """
    return SqlAlchemyUnitOfWork(
        async_sessionmaker(bind=engine, expire_on_commit=False),
        clock=FrozenClock(RECORDED),
    )


async def _insert(session: AsyncSession, record: AuditRecord) -> UUID:
    """Append one record through the repository and flush it to the database."""
    repository = AuditRepository(session, FACTORY)
    row_id = uuid4()
    await repository.add(record, row_id=row_id)
    await session.flush()
    return row_id


# --------------------------------------------------------------------------- #
# The three refusals. This is what ADR-071 decided.
# --------------------------------------------------------------------------- #


async def test_an_update_against_the_audit_table_fails(session: AsyncSession) -> None:
    """ADR-071's first required test.

    Raw SQL, not the repository. The repository has no ``update`` method, so
    calling it would prove nothing -- the claim is that the *database* refuses,
    for every role including the owner the application connects as.
    """
    row_id = await _insert(session, _record())

    with pytest.raises(DBAPIError) as raised:
        await session.execute(
            text("UPDATE audit_log SET outcome = 'failed' WHERE id = :id"), {"id": row_id}
        )

    assert raised.value.orig is not None
    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


async def test_a_delete_against_the_audit_table_fails(session: AsyncSession) -> None:
    """ADR-071's second required test.

    A record that can be deleted is not evidence that anything happened; it is
    evidence that nobody has deleted it yet.
    """
    row_id = await _insert(session, _record())

    with pytest.raises(DBAPIError) as raised:
        await session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})

    assert raised.value.orig is not None
    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


async def test_a_truncate_against_the_audit_table_fails(session: AsyncSession) -> None:
    """The refusal ADR-071 does not name, and which its two would not have caught.

    Row-level triggers do not fire on ``TRUNCATE``. A table guarded against only
    ``UPDATE`` and ``DELETE`` can therefore still be emptied in a single
    statement -- the same erasure, reached by a different verb, and the one that
    would be reached for precisely because it is fast.

    Plan §12 fixes retention at indefinite and immutable. That does not survive a
    table anyone can empty, so the statement-level guard closes it.
    """
    await _insert(session, _record())

    with pytest.raises(DBAPIError) as raised:
        await session.execute(text("TRUNCATE audit_log"))

    assert raised.value.orig is not None
    assert getattr(raised.value.orig, "sqlstate", None) == RESTRICT_VIOLATION


async def test_an_update_matching_no_rows_raises_nothing(session: AsyncSession) -> None:
    """The exact reach of a row-level guard, pinned so nobody misreads it.

    A ``BEFORE ... FOR EACH ROW`` trigger fires per matched row, so an ``UPDATE``
    matching nothing succeeds trivially and changes nothing. That is correct and
    worth stating, because "UPDATE on audit_log always errors" is the natural
    but wrong summary of the three tests above -- and someone relying on it would
    be surprised by a green statement that turns out to have matched no rows.

    It is also why ``TRUNCATE`` needed its own statement-level guard: a
    row-level trigger has nothing to fire on when there are no rows.
    """
    first = _record()
    await _insert(session, first)

    await session.execute(
        text("UPDATE audit_log SET outcome = 'failed' WHERE id = :id"),
        {"id": uuid4()},
    )
    second = _record()
    await _insert(session, second)

    await session.execute(
        text("UPDATE audit_log SET outcome = 'failed' WHERE id = :id"),
        {"id": uuid4()},
    )

    rows = await session.execute(
        text(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE outcome = 'failed' AND correlation_id IN (:first, :second)"
        ),
        {"first": first.correlation_id, "second": second.correlation_id},
    )

    assert rows.scalar_one() == 0


# --------------------------------------------------------------------------- #
# The row and its event share one transaction (ADR-071, ADR-053)
# --------------------------------------------------------------------------- #


async def test_recording_writes_the_row_and_stages_the_event_together(
    session: AsyncSession,
) -> None:
    """One call, both writes, one transaction.

    The sink is a fake here so the assertion is about *what* was staged and how
    it was routed. That both actually land in one transaction is the next test,
    which uses the real Unit of Work.
    """
    sink = RecordingSink()
    recorder = AuditRecorder(AuditRepository(session, FACTORY), sink)

    row_id = await recorder.record(_record())
    await session.flush()

    stored = await session.scalar(
        text("SELECT count(*) FROM audit_log WHERE id = :id"), {"id": row_id}
    )
    assert stored == 1
    assert len(sink.staged) == 1
    assert sink.staged[0][2] == AUDIT_AGGREGATE_TYPE
    assert sink.staged[0][1] == row_id


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_audit_row_and_its_event_share_one_transaction(
    migrated: AsyncEngine,
) -> None:
    """ADR-071's central claim, against the real Unit of Work and the real outbox.

    Both rows are asserted *inside* the transaction, then the transaction is
    failed and both are asserted gone. Doing it in that order is what makes the
    second half meaningful: a test that only checked afterwards would pass
    identically if nothing had ever been written.

    Nothing is committed, and that is deliberate rather than incidental. This
    table is guarded against ``TRUNCATE`` as well as ``DELETE``, so a committed
    audit row is permanent -- a suite that left one behind per run would grow a
    developer's database forever with rows about nothing.
    """
    record = _record()
    row_id: UUID | None = None

    # PT012: the block needs several statements because the *transaction* is the
    # subject -- the failure has to happen inside an open unit of work, which is
    # not expressible as a single call.
    with pytest.raises(RuntimeError, match="deliberate"):  # noqa: PT012
        async with _uow(migrated) as uow:
            recorder = AuditRecorder(AuditRepository(uow.session, FACTORY), uow)
            row_id = await recorder.record(record)
            await uow.session.flush()

            assert (
                await uow.session.scalar(
                    text("SELECT count(*) FROM audit_log WHERE id = :id"), {"id": row_id}
                )
                == 1
            )
            assert (
                await uow.session.scalar(
                    text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": row_id}
                )
                == 1
            )

            raise RuntimeError("deliberate")

    assert row_id is not None
    async with migrated.connect() as connection:
        audit_rows = await connection.scalar(
            text("SELECT count(*) FROM audit_log WHERE id = :id"), {"id": row_id}
        )
        outbox_rows = await connection.scalar(
            text("SELECT count(*) FROM outbox WHERE aggregate_id = :id"), {"id": row_id}
        )

    assert audit_rows == 0, "an audit record survived the action it recorded"
    assert outbox_rows == 0, "an event survived for work that did not happen"


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_published_event_survives_the_wire_format(migrated: AsyncEngine) -> None:
    """``AuditRecorded`` must be encodable by ADR-061's codecs, or it never leaves.

    The outbox serialiser refuses a type it has no codec for rather than falling
    back to ``str()``, which is the right choice and which makes this a real
    constraint on the event's fields. It is also why the event carries bare
    UUIDs: a :class:`SurrogateId` has no codec, and could not have one that
    decoded back to the right subclass.

    Asserted inside the transaction and rolled back, for the reason the previous
    test gives.
    """
    record = _record()

    with pytest.raises(RuntimeError, match="deliberate"):  # noqa: PT012 - see above
        async with _uow(migrated) as uow:
            recorder = AuditRecorder(AuditRepository(uow.session, FACTORY), uow)
            row_id = await recorder.record(record)
            await uow.session.flush()

            payload = await uow.session.scalar(
                text("SELECT payload FROM outbox WHERE aggregate_id = :id"), {"id": row_id}
            )
            decoded = {key: decode_value(value) for key, value in json.loads(payload).items()}

            assert decoded["actor"] == record.actor
            assert decoded["action"] == record.action.value
            assert decoded["account_id"] == record.account_id.value
            assert decoded["correlation_id"] == record.correlation_id
            assert decoded["occurred_at"] == record.occurred_at

            raise RuntimeError("deliberate")


# --------------------------------------------------------------------------- #
# The schema refuses what the domain refuses
# --------------------------------------------------------------------------- #


#: One INSERT, parameterised, used by every schema-level test below. Written out
#: rather than built from the model so that these tests exercise the table as an
#: operator would reach it, which is the only route the constraints exist for.
_INSERT_SQL: Final = text(
    "INSERT INTO audit_log "
    "(id, account_id, actor, action, subject, outcome, occurred_at, "
    " recorded_at, correlation_id) VALUES "
    "(:id, :account, :actor, :action, :subject, :outcome, :occurred, "
    " :recorded, :correlation)"
)


def _raw_row(**overrides: object) -> dict[str, object]:
    """Build parameters for :data:`_INSERT_SQL`, overriding one at a time."""
    defaults: dict[str, object] = {
        "id": uuid4(),
        "account": ACCOUNT.value,
        "actor": "operator@dhruva.local",
        "action": "authentication",
        "subject": "session",
        "outcome": "succeeded",
        "occurred": OCCURRED,
        "recorded": OCCURRED,
        "correlation": uuid4(),
    }
    return {**defaults, **overrides}


@pytest.mark.parametrize("column", ["actor", "subject"])
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_a_blank_actor_or_subject_is_refused_by_the_table(
    session: AsyncSession, column: str, blank: str
) -> None:
    """The domain rejects both; so does the table, for anything arriving by SQL.

    The check constraints are not redundant with the value object's invariants.
    They cover the routes the value object never sees -- a repair script, a bulk
    load, a future writer in another language -- which is the same argument the
    worked example's constraints make.

    The tab case is the one that found a defect. The constraint was first
    written as bare ``btrim(actor)``, which strips **spaces only** -- so a
    tab-only actor satisfied the table while the domain's ``.strip()`` rejected
    it, and the check was weaker than the invariant it mirrors on exactly the
    route it exists to cover. Parametrising over three kinds of blank is what
    surfaced that; a test using only ``''`` would have passed.
    """
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT_SQL, _raw_row(**{column: blank}))


@pytest.mark.parametrize("column", ["account", "actor", "correlation"])
async def test_a_missing_required_value_is_refused_by_the_table(
    session: AsyncSession, column: str
) -> None:
    """Every column ADR-071 names is NOT NULL, including the two that were not.

    ``account_id`` was nullable in the first draft of this migration and
    ``correlation_id`` was absent altogether. Both are named by ADR-071 -- the
    first via ADR-004, the second as what joins an audit entry to the request
    that caused it -- so both are asserted rather than assumed.
    """
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT_SQL, _raw_row(**{column: None}))


async def test_a_record_cannot_predate_the_action_it_records(session: AsyncSession) -> None:
    """A clock defect must not become a permanent, uncorrectable row.

    ``recorded_at`` before ``occurred_at`` means a skewed host or a naive local
    time converted wrongly, and an audit log built on a broken clock is one whose
    ordering cannot be trusted. The table refuses it, so the bad row never
    becomes evidence nobody can delete.
    """
    with pytest.raises(IntegrityError):
        await session.execute(_INSERT_SQL, _raw_row(recorded=OCCURRED - timedelta(seconds=1)))


async def test_the_table_carries_a_row_level_security_policy(session: AsyncSession) -> None:
    """ADR-074: every table with an ``account_id`` has a policy, authored from the start.

    Permissive in v1, so it changes no result today. Asserting its existence is
    what stops it from being discovered missing at S44 -- which, per ADR-074, is
    the worst possible day to find out.
    """
    policies = await session.scalar(
        text("SELECT count(*) FROM pg_policies WHERE tablename = 'audit_log'")
    )
    enabled = await session.scalar(
        text("SELECT relrowsecurity FROM pg_class WHERE relname = 'audit_log'")
    )

    assert policies == 1
    assert enabled is True


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


async def test_everything_under_one_correlation_comes_back_in_order(
    session: AsyncSession,
) -> None:
    """The query the correlation column exists for, and the one an incident asks.

    Oldest first, because the question is what the request did, in sequence.
    """
    correlation = uuid4()
    repository = AuditRepository(session, FACTORY)
    for index, action in enumerate(
        (AuditAction.AUTHENTICATION, AuditAction.CREDENTIAL_READ, AuditAction.ORDER_ACTION)
    ):
        moment = OCCURRED + timedelta(seconds=index)
        entry = _record(
            action=action,
            occurred_at=moment,
            recorded_at=moment,
            correlation_id=correlation,
        )
        await repository.add(entry, row_id=uuid4())
    await session.flush()

    found = await repository.for_correlation(correlation)

    assert [entry.action for entry in found] == [
        AuditAction.AUTHENTICATION,
        AuditAction.CREDENTIAL_READ,
        AuditAction.ORDER_ACTION,
    ]


async def test_a_read_by_account_does_not_return_another_tenant_s_rows(
    session: AsyncSession,
) -> None:
    """Application-level scoping today; RLS will make it structural at S44.

    Both belong here. ADR-074's policies are permissive in v1, so this filter is
    what actually isolates tenants right now -- and a test that only checked the
    policy existed would pass while the read returned everything.
    """
    repository = AuditRepository(session, FACTORY)
    await repository.add(_record(), row_id=uuid4())
    await repository.add(_record(account_id=OTHER_ACCOUNT), row_id=uuid4())
    await session.flush()

    found = await repository.for_account(ACCOUNT)

    assert found
    assert {entry.account_id for entry in found} == {ACCOUNT}


async def test_a_record_survives_a_round_trip_through_postgresql(session: AsyncSession) -> None:
    """Every field back exactly as written, including both timestamps.

    The value object compares by value, so this is one assertion rather than
    nine -- and a field lost in the mapping would fail it.
    """
    repository = AuditRepository(session, FACTORY)
    original = _record(recorded_at=OCCURRED + timedelta(minutes=3))
    row_id = uuid4()

    await repository.add(original, row_id=row_id)
    await session.flush()
    session.expunge_all()

    assert await repository.get(row_id) == original
