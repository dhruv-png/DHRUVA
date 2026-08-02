"""Fakes for testing persistence behaviour without a database.

A fake, not a mock: it records what happened and behaves like the real thing for
the operations under test. Mocks assert on calls; these let a test assert on
*outcomes*, which survives refactoring better.

None of this substitutes for the integration suite. Transactional semantics --
isolation, constraint timing, real rollback -- are only observable against a real
PostgreSQL (ADR-058). These fakes verify the Unit of Work's own logic: what it
calls, in what order, and on which paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

__all__ = ["FakeSession", "FakeSessionFactory"]


@dataclass
class FakeSession:
    """Records the transaction operations a Unit of Work performs on it."""

    calls: list[str] = field(default_factory=list)
    commit_error: Exception | None = None
    rollback_error: Exception | None = None
    closed: bool = False
    #: Objects staged into the session but not yet committed. A real session
    #: discards these on rollback, so this fake does too -- the Unit of Work's
    #: rollback semantics are now a property of the transaction rather than of a
    #: list it clears itself (AR-001b), and a fake that kept them would let a
    #: broken rollback pass.
    added: list[Any] = field(default_factory=list)
    executed: list[tuple[Any, dict[str, object] | None]] = field(default_factory=list)

    def add(self, instance: Any) -> None:
        """Stage an object, as ``AsyncSession.add`` does."""
        self.calls.append("add")
        self.added.append(instance)

    async def execute(
        self,
        statement: Any,
        parameters: dict[str, object] | None = None,
    ) -> None:
        """Record SQL issued directly by the transaction boundary."""
        self.calls.append("execute")
        self.executed.append((statement, parameters))

    async def commit(self) -> None:
        """Record a commit, or raise if the test asked for a failing one."""
        self.calls.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        """Record a rollback and discard anything staged, as a real one would."""
        self.calls.append("rollback")
        self.added.clear()
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self) -> None:
        """Record disposal."""
        self.calls.append("close")
        self.closed = True


class FakeSessionFactory:
    """Hands out :class:`FakeSession` instances and remembers each one.

    Remembering them is what lets a test assert that a *new* session was created
    per Unit of Work, which is the property ADR-056 requires.
    """

    def __init__(self) -> None:
        """Create a factory with no sessions issued yet."""
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        """Issue a fresh session."""
        session = FakeSession()
        self.sessions.append(session)
        return session

    @property
    def latest(self) -> FakeSession:
        """Return the most recently issued session."""
        return self.sessions[-1]


@dataclass
class CommittingRepository:
    """A deliberately misbehaving repository that tries to commit.

    Exists to prove the architecture forbids this, rather than to be used.
    """

    session: Any

    async def naughty_commit(self) -> None:
        """Commit outside the Unit of Work, which no real repository may do."""
        await self.session.commit()


@dataclass
class TrackingUnitOfWorkUser:
    """A stand-in use case, so tests can exercise realistic call sequences."""

    entered: bool = False
    exited: bool = False

    async def __aenter__(self) -> Self:
        """Mark entry."""
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Mark exit."""
        self.exited = True
