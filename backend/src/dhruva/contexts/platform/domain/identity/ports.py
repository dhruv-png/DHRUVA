"""The contracts an identity use case depends on (ADR-052, ADR-053, ADR-059).

Why these exist at all
----------------------
S06's token step introduces the **first application-layer use case in the
repository**. Until now every context's ``application`` package has been empty,
so the question of what a use case is allowed to depend on has never had to be
answered concretely.

The layering answers it: ``application`` may import this context's ``domain`` and
``dhruva.shared``, and may not import ``infrastructure``. A use case therefore
cannot hold a ``PrincipalRepository`` — that class is SQLAlchemy — and cannot
hold a ``SqlAlchemyUnitOfWork`` either. It holds these protocols, and the
composition root supplies the adapters.

Why not reuse ``dhruva.shared.persistence.Repository``
------------------------------------------------------
That protocol is generic over ``get``/``add``/``update`` by identity, which is
the right shape for an aggregate addressed by its own id. Neither of these is:
authentication looks a principal up by **subject**, and refresh looks a token up
by **hash**, because in both cases the identity is the thing being established
rather than something the caller already holds. Declaring the real access paths
here is more honest than inheriting three methods and adding the two that matter.

Why the transaction is a protocol too
-------------------------------------
:class:`IdentityUnitOfWork` follows the shape
:class:`dhruva.shared.persistence.UnitOfWork` documents — repositories reached as
attributes of the transaction that owns them. That is what keeps "one use case,
one transaction" (ADR-053) true by construction: there is no way to obtain a
repository except from a unit of work, so there is no way to write outside one.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from dhruva.contexts.platform.domain.audit import AuditRecord
    from dhruva.contexts.platform.domain.identity.authorisation import Role
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId, PrincipalId

__all__ = [
    "AuditSink",
    "AuthorisationDirectory",
    "IdentityUnitOfWork",
    "MintedRefreshToken",
    "PrincipalStore",
    "RefreshTokenMinter",
    "RefreshTokenStore",
    "RoleStore",
]


@runtime_checkable
class AuthorisationDirectory(Protocol):
    """Transaction-scoped queries needed to preserve authority invariants."""

    async def serialise(self, account_id: AccountId) -> None:
        """Serialise authority changes for one tenant until transaction end."""
        ...

    async def holders(self, account_id: AccountId, role_name: str) -> tuple[Principal, ...]:
        """Return every principal currently assigned to the named role."""
        ...

    async def count_active_managers(
        self,
        account_id: AccountId,
        *,
        excluding_role_name: str | None = None,
    ) -> int:
        """Count active, TOTP-enrolled managers outside an optional role."""
        ...


@dataclass(frozen=True, slots=True)
class MintedRefreshToken:
    """A freshly generated refresh token: the secret, and what gets stored.

    The only moment both exist together. The secret is returned to the client and
    then forgotten; the digest is what the row keeps, so that a stolen database
    is a set of useless hashes rather than a set of live sessions (ADR-033).

    Attributes
    ----------
    secret
        The value the client presents on the next refresh. A
        :class:`~dhruva.shared.config.secret.SecretValue`, so it is registered
        for redaction and a copy reaching a log line is masked (ADR-037).
    digest
        What :meth:`RefreshTokenStore.get_by_hash` matches on.
    """

    secret: SecretValue
    digest: bytes


@runtime_checkable
class RefreshTokenMinter(Protocol):
    """Generates refresh tokens and digests presented ones (ADR-072).

    A port rather than a stdlib call in the use case, for the reason every other
    generator in this platform is one: the use case should be reproducible under
    test, and "the token was random" is not something a test can assert about a
    value it cannot predict. It also keeps the digest algorithm in one place, so
    that minting and presentation cannot come to disagree about it -- which would
    present as every refresh failing, for everyone, at once.
    """

    def mint(self) -> MintedRefreshToken:
        """Generate a new refresh token and its digest."""
        ...

    def digest(self, presented: SecretValue) -> bytes:
        """Return the digest of a token a client presented.

        Must agree with :meth:`mint` exactly. This is the lookup key, so a
        disagreement is not a subtle failure -- no session in the platform would
        refresh again.
        """
        ...


@runtime_checkable
class PrincipalStore(Protocol):
    """Loads and stores :class:`Principal` aggregates. Never commits (ADR-053)."""

    async def get_by_subject(self, subject: str) -> Principal | None:
        """Return the principal with this login subject, or ``None``.

        ``None`` rather than raising: "nobody by that name" is the ordinary
        answer on a login form, and ADR-072 requires the caller to render it
        identically to a wrong password anyway.
        """
        ...

    async def get(self, principal_id: PrincipalId) -> Principal | None:
        """Return the principal with this identity, or ``None``.

        The path a refresh takes: the token names its principal, and the session
        may only continue if that principal is still active.
        """
        ...

    async def add(self, principal: Principal) -> None:
        """Stage a new principal for insertion."""
        ...

    async def update(self, principal: Principal) -> None:
        """Stage changes, refusing the write if another writer got there first.

        Raises
        ------
        ConflictError
            If the stored version no longer matches (ADR-057).
        """
        ...


@runtime_checkable
class RefreshTokenStore(Protocol):
    """Loads and stores :class:`RefreshToken` aggregates. Never commits."""

    async def get_by_hash(self, token_hash: bytes) -> RefreshToken | None:
        """Return the token with this hash, or ``None``.

        The only lookup presentation performs. A hash that matches nothing is a
        token this platform never issued, or one from a database that has since
        been rebuilt; either way it is an authentication failure and not an
        error.
        """
        ...

    async def add(self, token: RefreshToken) -> None:
        """Stage a newly issued token for insertion."""
        ...

    async def mark_replaced(self, token: RefreshToken) -> bool:
        """Stage the predecessor half of a rotation, and report whether it won.

        Returns
        -------
        bool
            ``True`` if this call closed the token; ``False`` if it was already
            closed, meaning a concurrent request rotated it first.

        Notes
        -----
        The boolean is the concurrency control, and it is why
        :class:`RefreshToken` needs no ``version`` column. The update matches on
        ``replaced_by IS NULL``, so exactly one of two simultaneous rotations
        affects a row. The loser must not treat its own defeat as reuse — it
        holds a token that was current when it read it — so the distinction is
        reported rather than raised.
        """
        ...

    async def revoke_lineage(self, lineage_id: UUID, *, at: datetime) -> int:
        """Revoke every unrevoked token in one rotation chain.

        Returns
        -------
        int
            How many tokens this revoked. Zero means the lineage was already
            dead, which makes repeated detection cheap and idempotent.

        Notes
        -----
        One statement against one indexed column, never a recursive walk. The
        walk would be slowest for the longest-lived chain, which is both the most
        valuable to a thief and the one most likely to be under active abuse when
        this is called.
        """
        ...


@runtime_checkable
class RoleStore(Protocol):
    """Loads and stores tenant-scoped :class:`Role` aggregates. Never commits."""

    async def get(self, account_id: AccountId, name: str) -> Role | None:
        """Return the named role in one tenant, or ``None``."""
        ...

    async def get_for_principal(self, principal_id: PrincipalId) -> Role | None:
        """Return the principal's single assigned role, or ``None``.

        ``None`` is deny-by-default's ordinary input: an unassigned principal
        holds no permissions and is not an infrastructure error.
        """
        ...

    async def add(self, role: Role) -> None:
        """Stage a new role and all of its live permission grants."""
        ...

    async def update(self, role: Role) -> None:
        """Stage a changed role, refusing a concurrent lost update."""
        ...


@runtime_checkable
class AuditSink(Protocol):
    """Records an audited action and publishes that it happened (ADR-071).

    Structurally satisfied by
    :class:`~dhruva.contexts.platform.infrastructure.audit.AuditRecorder`.
    Declared as a port because the application layer may not import that class,
    and because a use case should not be able to write the audit row without
    also staging its event — which the adapter guarantees and this signature
    inherits by having no method that does only one.
    """

    async def record(self, record: AuditRecord) -> UUID:
        """Write the audit row and stage ``AuditRecorded``, in this transaction."""
        ...


@runtime_checkable
class IdentityUnitOfWork(Protocol):
    """One transaction, and the identity stores participating in it (ADR-053).

    Examples
    --------
    ::

        async with unit_of_work_factory() as uow:
            principal = await uow.principals.get_by_subject(subject)
            ...
            await uow.audit.record(entry)
            await uow.commit()

    Nesting is refused rather than joined by the adapter, and leaving the block
    without :meth:`commit` rolls back — both inherited from
    :class:`~...infrastructure.database.unit_of_work.SqlAlchemyUnitOfWork`, which
    this wraps.
    """

    @property
    def principals(self) -> PrincipalStore:
        """The principal store bound to this transaction."""
        ...

    @property
    def refresh_tokens(self) -> RefreshTokenStore:
        """The refresh-token store bound to this transaction."""
        ...

    @property
    def roles(self) -> RoleStore:
        """The role store bound to this transaction."""
        ...

    @property
    def authorisation(self) -> AuthorisationDirectory:
        """Authority invariant queries bound to this transaction."""
        ...

    @property
    def audit(self) -> AuditSink:
        """The audit recorder bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` succeeded."""
        ...

    async def commit(self) -> None:
        """Commit the transaction, including any staged outbox rows."""
        ...
