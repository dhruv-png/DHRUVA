"""Unit of Work behaviour (ADR-053).

Five properties, each one a way the transaction boundary could silently do the
wrong thing:

1. a successful commit commits;
2. an exception rolls back;
3. nesting is refused rather than joined;
4. the session is disposed on every path;
5. a repository cannot commit outside the boundary.

These run without a database. What they verify is the Unit of Work's own logic --
what it calls, in what order, on which paths. Real transactional semantics are an
integration concern and are covered separately (ADR-058).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.persistence.outbox import OutboxRow
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.events import DomainEvent
from dhruva.shared.time import FrozenClock
from tests.unit.persistence.fakes import (
    CommittingRepository,
    FakeSessionFactory,
)

pytestmark = pytest.mark.unit


class _Happened(DomainEvent):
    """A minimal fact, for testing publication."""


def _event() -> _Happened:
    return _Happened(occurred_at=datetime(2026, 7, 28, tzinfo=UTC))


def _uow(factory: FakeSessionFactory) -> Any:
    return SqlAlchemyUnitOfWork(cast("Any", factory))


# --------------------------------------------------------------------------- #
# 1. Successful commit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_successful_commit_commits_then_closes() -> None:
    """The ordinary path, asserted on call order rather than on a mock."""
    factory = FakeSessionFactory()

    async with _uow(factory) as uow:
        await uow.commit()

    assert factory.latest.calls == ["commit", "close"]


@pytest.mark.asyncio
async def test_committing_does_not_also_roll_back() -> None:
    """A rollback after a successful commit would discard nothing but says a lot.

    Its presence would mean the Unit of Work does not know whether it committed,
    which is the state that produces silent partial writes.
    """
    factory = FakeSessionFactory()

    async with _uow(factory) as uow:
        await uow.commit()

    assert "rollback" not in factory.latest.calls


# --------------------------------------------------------------------------- #
# 2. Rollback
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_leaving_without_committing_rolls_back() -> None:
    """Rollback is the default, not the exception.

    Committing on clean exit would mean a use case returning early commits
    partial work, silently.
    """
    factory = FakeSessionFactory()

    async with _uow(factory):
        pass

    assert factory.latest.calls == ["rollback", "close"]


@pytest.mark.asyncio
async def test_an_exception_rolls_back_and_propagates() -> None:
    """The caller must still see their error; the Unit of Work is not a handler."""
    factory = FakeSessionFactory()

    with pytest.raises(RuntimeError, match="boom"):
        async with _uow(factory):
            raise RuntimeError("boom")

    assert factory.latest.calls == ["rollback", "close"]


@pytest.mark.asyncio
async def test_an_exception_after_commit_does_not_roll_back_the_commit() -> None:
    """Once committed, the work is durable; a later failure cannot undo it.

    Asserting this stops a future refactor from adding a defensive rollback that
    would silently attempt to reverse a completed transaction.
    """
    factory = FakeSessionFactory()

    with pytest.raises(RuntimeError):
        async with _uow(factory) as uow:
            await uow.commit()
            raise RuntimeError("after commit")

    assert factory.latest.calls == ["commit", "close"]


# --------------------------------------------------------------------------- #
# 3. Nesting
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nesting_is_refused_rather_than_joined() -> None:
    """Joining would make an inner commit a no-op that appears to succeed."""
    factory = FakeSessionFactory()
    uow = _uow(factory)

    async with uow:
        with pytest.raises(InvariantViolation, match="already active"):
            async with uow:
                pass


@pytest.mark.asyncio
async def test_a_unit_of_work_can_be_reused_sequentially() -> None:
    """Refusing nesting must not prevent a second, separate transaction.

    Two sessions, not one reused: a session outliving its transaction is the
    ambient-session problem ADR-056 removes.
    """
    factory = FakeSessionFactory()
    uow = _uow(factory)

    async with uow:
        await uow.commit()
    async with uow:
        await uow.commit()

    assert len(factory.sessions) == 2
    assert all(session.calls == ["commit", "close"] for session in factory.sessions)


# --------------------------------------------------------------------------- #
# 4. Session disposal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_inside", [False, True], ids=["clean", "exception"])
async def test_the_session_is_always_closed(raise_inside: bool) -> None:
    """Every path disposes of the session. A leaked session holds a pool slot."""
    factory = FakeSessionFactory()

    if raise_inside:
        with pytest.raises(RuntimeError):
            async with _uow(factory):
                raise RuntimeError("boom")
    else:
        async with _uow(factory):
            pass

    assert factory.latest.closed


@pytest.mark.asyncio
async def test_the_session_is_closed_even_when_commit_itself_fails() -> None:
    """The nastiest path: the commit raises, so nothing is committed or rolled back.

    Without the ``finally``, this leaks a connection on exactly the occasion the
    database is already under stress.
    """
    factory = FakeSessionFactory()
    uow = _uow(factory)

    with pytest.raises(ValueError, match="deadlock"):
        async with uow:
            factory.latest.commit_error = ValueError("deadlock")
            await uow.commit()

    assert factory.latest.closed
    assert factory.latest.calls == ["commit", "rollback", "close"]


@pytest.mark.asyncio
async def test_the_session_is_unreachable_outside_the_boundary() -> None:
    """A session with no defined lifetime is the ambient-session problem."""
    uow = _uow(FakeSessionFactory())

    with pytest.raises(InvariantViolation, match="not active"):
        _ = uow.session

    async with uow:
        assert uow.session is not None

    with pytest.raises(InvariantViolation, match="not active"):
        _ = uow.session


# --------------------------------------------------------------------------- #
# 5. Repositories must not commit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_repository_committing_is_visible_as_an_extra_commit() -> None:
    """Proves the failure this architecture forbids is detectable.

    Nothing at runtime stops a badly written repository calling ``commit``. What
    the architecture provides is that the protocol has no such method (asserted
    in test_persistence_contracts) and that the effect is observable here.
    """
    factory = FakeSessionFactory()

    async with _uow(factory) as uow:
        await CommittingRepository(uow.session).naughty_commit()
        await uow.commit()

    assert factory.latest.calls.count("commit") == 2, (
        "a repository commit is an extra, unaccounted transaction boundary"
    )


# --------------------------------------------------------------------------- #
# Domain event staging (AR-001b)
# --------------------------------------------------------------------------- #
#
# These assert on outbox rows in the session rather than on a publisher, because
# `add_event` now writes into the transaction instead of holding events in memory
# for publication after commit. The guarantees are the same ones the previous
# tests covered; they are simply enforced by the transaction now, which is
# stronger -- the earlier version could not test the window between a commit
# returning and an in-process publish completing, because with a fake session
# there was no durability boundary to crash between.


def _staged_rows(factory: FakeSessionFactory) -> list[Any]:
    """Return the outbox rows staged into the current session."""
    return [row for row in factory.latest.added if isinstance(row, OutboxRow)]


@pytest.mark.asyncio
async def test_an_event_is_staged_into_the_transaction_not_published() -> None:
    """The event row is written to the session, so it shares the work's fate."""
    factory = FakeSessionFactory()

    async with _uow(factory) as uow:
        uow.add_event(_event())

        rows = _staged_rows(factory)
        assert len(rows) == 1, "event was not staged into the session"
        await uow.commit()

    assert "commit" in factory.latest.calls


@pytest.mark.asyncio
async def test_a_staged_event_is_discarded_when_the_transaction_rolls_back() -> None:
    """Nothing observable happened, so nothing should be observed."""
    factory = FakeSessionFactory()

    with pytest.raises(RuntimeError):
        async with _uow(factory) as uow:
            uow.add_event(_event())
            raise RuntimeError("boom")

    assert _staged_rows(factory) == []
    assert "rollback" in factory.latest.calls


@pytest.mark.asyncio
async def test_a_staged_event_is_discarded_when_the_commit_fails() -> None:
    """The commit raised, so the work did not happen and neither did the event."""
    factory = FakeSessionFactory()
    uow = _uow(factory)

    with pytest.raises(ValueError, match="constraint violation"):
        async with uow:
            factory.latest.commit_error = ValueError("constraint violation")
            uow.add_event(_event())
            await uow.commit()

    assert _staged_rows(factory) == []


@pytest.mark.asyncio
async def test_events_do_not_leak_between_transactions() -> None:
    """A staged event from a rolled-back transaction must not reappear."""
    factory = FakeSessionFactory()
    uow = _uow(factory)

    with pytest.raises(RuntimeError):
        async with uow:
            uow.add_event(_event())
            raise RuntimeError("boom")

    async with uow:
        await uow.commit()

    assert _staged_rows(factory) == [], "a second transaction inherited a staged event"


@pytest.mark.asyncio
async def test_staged_events_are_visible_before_commit() -> None:
    """So a use case can assert on what it raised without reaching into the bus."""
    factory = FakeSessionFactory()

    async with _uow(factory) as uow:
        uow.add_event(_event())

        assert len(uow.staged_events) == 1


@pytest.mark.asyncio
async def test_the_staged_row_carries_the_bitemporal_pair_from_the_clock() -> None:
    """`occurred_at` is the event's; `recorded_at` comes from the injected clock.

    Conflating them is how lookahead bias enters a backtest (ADR-007), and taking
    `recorded_at` from a wall clock rather than an injected one is what would make
    the `as_of` bound of ADR-069 unenforceable in replay.
    """
    factory = FakeSessionFactory()
    frozen = datetime(2026, 7, 29, 10, 30, tzinfo=UTC)
    event = _event()

    async with SqlAlchemyUnitOfWork(cast("Any", factory), clock=FrozenClock(frozen)) as uow:
        uow.add_event(event)

        # Read inside the block: leaving it rolls back, and a rollback discards
        # staged rows. Asserting afterwards would pass vacuously on an empty
        # list, which is the shape of a test that checks nothing.
        row = _staged_rows(factory)[0]
        assert row.recorded_at == frozen
        assert row.occurred_at == event.occurred_at
        assert row.recorded_at != row.occurred_at


@pytest.mark.asyncio
async def test_the_staged_row_carries_aggregate_and_account_scope() -> None:
    """Per-aggregate ordering (ADR-062) and tenant scope (ADR-004) need these."""
    factory = FakeSessionFactory()
    aggregate, account = uuid4(), uuid4()

    async with _uow(factory) as uow:
        uow.add_event(_event(), aggregate_id=aggregate, account_id=account)

        row = _staged_rows(factory)[0]
        assert row.aggregate_id == aggregate
        assert row.account_id == account
