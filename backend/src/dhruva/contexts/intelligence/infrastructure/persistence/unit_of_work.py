"""Intelligence transaction boundary with account-scoped session context."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

from sqlalchemy import text

from dhruva.contexts.intelligence.infrastructure.persistence.repository import (
    CandidateObservationRepository,
    CandidateOutcomeRepository,
    NewsRepository,
    ResearchObservationRepository,
)
from dhruva.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.shared.identity import AccountId

__all__ = ["SqlAlchemyIntelligenceUnitOfWork"]

_SESSION_ACCOUNT_SETTING = "dhruva.current_account_id"


class SqlAlchemyIntelligenceUnitOfWork:
    """One caller-owned transaction exposing only news facts.

    News is global reference evidence rather than tenant state -- both users read
    the same headlines -- but the transaction still carries the requesting
    account, exactly as the instrument archive does, so an audited read is
    attributable to whoever asked for it.
    """

    __slots__ = ("_account_id", "_committed", "_session", "_session_factory")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        account_id: AccountId,
    ) -> None:
        """Bind a fresh session factory and mandatory tenant identity."""
        self._session_factory = session_factory
        self._account_id = account_id
        self._session: AsyncSession | None = None
        self._committed = False

    @property
    def session(self) -> AsyncSession:
        """Return the active session and refuse ambient access."""
        if self._session is None:
            raise InvariantViolation("unit of work is not active")
        return self._session

    @property
    def news(self) -> NewsRepository:
        """Return the news repository bound to this transaction."""
        return NewsRepository(self.session)

    @property
    def observations(self) -> ResearchObservationRepository:
        """Return the account-scoped research-observation repository."""
        return ResearchObservationRepository(self.session, account_id=self._account_id)

    @property
    def candidate_observations(self) -> CandidateObservationRepository:
        """Return the append-only experimental candidate freeze store."""
        return CandidateObservationRepository(self.session, account_id=self._account_id)

    @property
    def candidate_outcomes(self) -> CandidateOutcomeRepository:
        """Return the append-only matured candidate outcome store."""
        return CandidateOutcomeRepository(self.session, account_id=self._account_id)

    async def __aenter__(self) -> Self:
        """Open a session and set transaction-local tenant context."""
        if self._session is not None:
            raise InvariantViolation("unit of work nesting is not permitted")
        self._session = self._session_factory()
        self._committed = False
        await self._session.execute(
            text("SELECT set_config(:setting, :account_id, true)"),
            {"setting": _SESSION_ACCOUNT_SETTING, "account_id": str(self._account_id.value)},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back by default and always close the session."""
        session = self._session
        if session is None:  # pragma: no cover - context manager makes this unreachable
            return
        try:
            if not self._committed:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
            self._committed = False

    async def commit(self) -> None:
        """Commit every staged news revision and analysis."""
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """Discard every staged news revision and analysis."""
        await self.session.rollback()
