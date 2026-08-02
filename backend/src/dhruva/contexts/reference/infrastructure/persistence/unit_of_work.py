"""Reference transaction boundary with tenant-scoped PostgreSQL context."""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

from sqlalchemy import text

from dhruva.contexts.reference.infrastructure.persistence.instrument_archive import (
    InstrumentArchiveRepository,
)
from dhruva.contexts.reference.infrastructure.persistence.repository import ReferenceRepository
from dhruva.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.shared.identity import AccountId

__all__ = ["SqlAlchemyReferenceUnitOfWork"]

_SESSION_ACCOUNT_SETTING = "dhruva.current_account_id"


class SqlAlchemyReferenceUnitOfWork:
    """One SQLAlchemy transaction exposing only the reference repository."""

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
        """Return the active session, refusing ambient access."""
        if self._session is None:
            raise InvariantViolation("unit of work is not active")
        return self._session

    @property
    def reference(self) -> ReferenceRepository:
        """Return the reference store bound to this transaction."""
        return ReferenceRepository(self.session)

    @property
    def instrument_archive(self) -> InstrumentArchiveRepository:
        """Return the provider archive store bound to this transaction."""
        return InstrumentArchiveRepository(self.session)

    async def __aenter__(self) -> Self:
        """Open a session and set transaction-local tenant context."""
        if self._session is not None:
            raise InvariantViolation("unit of work nesting is not permitted")
        self._session = self._session_factory()
        self._committed = False
        await self._session.execute(
            text("SELECT set_config(:setting, :account_id, true)"),
            {
                "setting": _SESSION_ACCOUNT_SETTING,
                "account_id": str(self._account_id.value),
            },
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
        """Commit every staged reference revision."""
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """Discard every staged reference revision."""
        await self.session.rollback()
