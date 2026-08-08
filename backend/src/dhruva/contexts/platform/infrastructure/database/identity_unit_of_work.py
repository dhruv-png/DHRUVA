"""One transaction, and the identity stores inside it (ADR-053, ADR-071).

Composition rather than inheritance: this wraps
:class:`~...database.unit_of_work.SqlAlchemyUnitOfWork` rather than extending it,
because the transaction semantics being inherited are exactly the ones that must
not be adjusted. Rollback by default, nesting refused rather than joined, events
staged into the outbox inside the transaction, session always disposed -- all of
those stay where they are tested, and this adds repositories on top.

Why the stores are properties and not constructor arguments
-----------------------------------------------------------
A repository needs the session, and the session does not exist until the block is
entered. Building them lazily against :attr:`SqlAlchemyUnitOfWork.session` means
there is no way to obtain a store outside an open transaction: the session
property raises there, so a caller who tried would get the ambient-session error
rather than a repository quietly bound to nothing.

That is the same reasoning the shared ``UnitOfWork`` protocol documents, made
enforceable.

Why the audit recorder is here too
----------------------------------
ADR-071 requires the audit row to share the transaction of the action it records.
Reaching it through the same object that owns the transaction is what makes that
structural: there is no ``AuditRecorder`` a use case could construct against some
other session, because the use case never sees a session at all.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Self

from dhruva.contexts.platform.infrastructure.audit import AuditRecorder
from dhruva.contexts.platform.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from dhruva.contexts.platform.infrastructure.persistence.audit import AuditRepository
from dhruva.contexts.platform.infrastructure.persistence.authorisation import (
    PostgresAuthorisationDirectory,
    RoleRepository,
)
from dhruva.contexts.platform.infrastructure.persistence.credentials import (
    CredentialRepository,
)
from dhruva.contexts.platform.infrastructure.persistence.factories import (
    AuditFactory,
    CredentialFactory,
    PrincipalFactory,
    RefreshTokenFactory,
    RoleFactory,
)
from dhruva.contexts.platform.infrastructure.persistence.identity import (
    PrincipalRepository,
    RefreshTokenRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.shared.identity import AccountId
    from dhruva.shared.time import Clock

__all__ = ["SqlAlchemyIdentityUnitOfWork"]


class SqlAlchemyIdentityUnitOfWork:
    """A transaction exposing the principal, refresh-token and audit stores.

    Satisfies
    :class:`~dhruva.contexts.platform.domain.identity.ports.IdentityUnitOfWork`
    structurally.

    Examples
    --------
    ::

        async with SqlAlchemyIdentityUnitOfWork(session_factory, clock=clock) as uow:
            principal = await uow.principals.get_by_subject(subject)
            ...
            await uow.audit.record(entry)
            await uow.commit()
    """

    __slots__ = (
        "_audit_factory",
        "_credential_factory",
        "_inner",
        "_principal_factory",
        "_refresh_factory",
        "_role_factory",
    )

    def __init__(  # noqa: PLR0913 - explicit factories are composition-root dependencies
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
        account_id: AccountId | None = None,
        principal_factory: PrincipalFactory | None = None,
        refresh_factory: RefreshTokenFactory | None = None,
        role_factory: RoleFactory | None = None,
        audit_factory: AuditFactory | None = None,
        credential_factory: CredentialFactory | None = None,
    ) -> None:
        """Create a Unit of Work over a session factory.

        The reconstruction factories are stateless and default to fresh
        instances. They are still injectable, because a factory is where a domain
        service would be held if one were ever needed -- the worked example's
        calendar is exactly that -- and a constructor that could not accept one
        would have to be changed the first time it was.
        """
        self._inner = SqlAlchemyUnitOfWork(
            session_factory,
            clock=clock,
            account_id=account_id,
        )
        self._principal_factory = principal_factory or PrincipalFactory()
        self._refresh_factory = refresh_factory or RefreshTokenFactory()
        self._role_factory = role_factory or RoleFactory()
        self._audit_factory = audit_factory or AuditFactory()
        self._credential_factory = credential_factory or CredentialFactory()

    @property
    def session(self) -> AsyncSession:
        """Return the active session.

        Exposed for tests and for the composition root. A use case never touches
        it -- the application layer cannot even name the type.
        """
        return self._inner.session

    @property
    def principals(self) -> PrincipalRepository:
        """The principal store bound to this transaction."""
        return PrincipalRepository(self.session, self._principal_factory)

    @property
    def credentials(self) -> CredentialRepository:
        """The sealed broker-credential store bound to this transaction.

        Built with no ``KeyProvider``, like every other repository here. Sealing
        and opening are the caller's explicit acts (ADR-070), so a transaction
        cannot quietly acquire the ability to decrypt by holding a store.
        """
        return CredentialRepository(self.session, self._credential_factory)

    @property
    def refresh_tokens(self) -> RefreshTokenRepository:
        """The refresh-token store bound to this transaction."""
        return RefreshTokenRepository(self.session, self._refresh_factory)

    @property
    def roles(self) -> RoleRepository:
        """The tenant-scoped role store bound to this transaction."""
        return RoleRepository(self.session, self._role_factory)

    @property
    def authorisation(self) -> PostgresAuthorisationDirectory:
        """Authority invariant queries bound to this transaction."""
        return PostgresAuthorisationDirectory(self.session, self._principal_factory)

    @property
    def audit(self) -> AuditRecorder:
        """The audit recorder bound to this transaction.

        Writes the row and stages ``AuditRecorded`` into this transaction's
        outbox, so neither can happen without the other and both share the fate
        of the action being audited (ADR-071).
        """
        return AuditRecorder(AuditRepository(self.session, self._audit_factory), self._inner)

    async def __aenter__(self) -> Self:
        """Open a session and begin a transaction."""
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless committed, then always dispose of the session."""
        await self._inner.__aexit__(exc_type, exc, traceback)

    async def commit(self) -> None:
        """Commit the transaction, including any staged outbox rows."""
        await self._inner.commit()

    async def rollback(self) -> None:
        """Discard everything staged in this transaction, including events."""
        await self._inner.rollback()
