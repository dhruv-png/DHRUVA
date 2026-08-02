"""Repositories for principals and refresh tokens (ADR-072, ADR-053, ADR-057).

Both satisfy the ports in ``domain.identity.ports`` structurally, and both obey
the rule every repository in this platform obeys: **they never commit**.
Transaction lifetime belongs to the Unit of Work, which on the authentication
path is what lets a *failed* login write its audit row and commit while the use
case then raises (ADR-071, ADR-072).

Two concurrency stories, deliberately different
-----------------------------------------------
:class:`PrincipalRepository` uses ADR-057's optimistic concurrency: a version
column, an ``UPDATE ... WHERE version = :loaded``, and a ``ConflictError`` when
another writer won. That is right for an aggregate whose fields change
independently -- two concurrent password changes must not silently merge.

:class:`RefreshTokenRepository` does not, and the absence is reasoned rather than
an omission. Rotation is a single conditional update -- close this token *if it
is still open* -- so the predicate is already the concurrency control, and the
loser needs to learn that it lost rather than to retry. A version column would be
a second mechanism for a race the first one already resolves correctly, and worse
it would make the two rotations look like a lost update rather than like what
they are: one winner and one client that should be told to authenticate again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select, update

from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_principal_model_kwargs,
    to_principal_record,
    to_refresh_token_model_kwargs,
    to_refresh_token_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalModel,
    RefreshTokenModel,
)
from dhruva.shared.errors import ConflictError

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
    from dhruva.contexts.platform.infrastructure.persistence.factories import (
        PrincipalFactory,
        RefreshTokenFactory,
    )
    from dhruva.shared.identity import PrincipalId

__all__ = ["PrincipalRepository", "RefreshTokenRepository"]


class PrincipalRepository:
    """Loads and stores :class:`Principal` aggregates."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: PrincipalFactory) -> None:
        """Bind the repository to a session and a reconstruction factory."""
        self._session = session
        self._factory = factory

    async def get_by_subject(self, subject: str) -> Principal | None:
        """Return the principal with this login subject, or ``None``.

        The authentication path's only lookup. ``None`` rather than raising:
        ADR-072 requires the caller to render "no such subject" exactly as it
        renders "wrong password", so an exception here would only have to be
        caught and flattened one layer up.
        """
        statement = select(PrincipalModel).where(PrincipalModel.subject == subject)
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        return self._factory.reconstruct(to_principal_record(model))

    async def get(self, principal_id: PrincipalId) -> Principal | None:
        """Return the principal with this identity, or ``None``.

        The refresh path's lookup: a presented token names its principal, and
        the session may only continue if that principal still exists and is
        still active.
        """
        model = await self._session.get(PrincipalModel, principal_id.value)
        if model is None:
            return None
        return self._factory.reconstruct(to_principal_record(model))

    async def add(self, principal: Principal) -> None:
        """Stage a new principal for insertion."""
        record = self._factory.deconstruct(principal)
        self._session.add(PrincipalModel(**to_principal_model_kwargs(record)))

    async def update(self, principal: Principal) -> None:
        """Stage changes, refusing the write if another writer got there first.

        Raises
        ------
        ConflictError
            If the stored version no longer matches (ADR-057). Retry belongs to
            the caller and is rarely right here: two concurrent password changes
            resolving by retry would apply whichever arrived second, which is not
            obviously the one the operator meant.

        Notes
        -----
        ``subject`` and ``account_id`` are absent from the ``SET`` clause on
        purpose, matching how the credential repository refuses to move its
        binding columns. Changing a principal's subject would orphan every audit
        record naming the old one, and moving a principal between accounts would
        carry its refresh tokens into another tenant. Both are new principals
        rather than updates, and the statement has no way to express either.
        """
        previous_version = principal.version - 1
        record = self._factory.deconstruct(principal)
        statement = (
            update(PrincipalModel)
            .where(
                PrincipalModel.id == record.id,
                PrincipalModel.version == previous_version,
            )
            .values(
                password_hash=record.password_hash,
                totp_secret=record.totp_secret,
                totp_wrapped_key=record.totp_wrapped_key,
                disabled_at=record.disabled_at,
                updated_at=record.updated_at,
                version=record.version,
            )
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        if result.rowcount == 0:
            msg = "principal was modified by another writer"
            raise ConflictError(
                msg,
                principal_id=str(principal.principal_id),
                expected_version=previous_version,
            )


class RefreshTokenRepository:
    """Loads, issues, closes and revokes :class:`RefreshToken` aggregates."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: RefreshTokenFactory) -> None:
        """Bind the repository to a session and a reconstruction factory."""
        self._session = session
        self._factory = factory

    async def get_by_hash(self, token_hash: bytes) -> RefreshToken | None:
        """Return the token with this hash, or ``None``.

        The only lookup presentation performs, and the reason the column is
        unique. A hash matching nothing is a token this platform never issued --
        or one from a database since rebuilt -- which is an authentication
        failure rather than an error.
        """
        statement = select(RefreshTokenModel).where(RefreshTokenModel.token_hash == token_hash)
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        return self._factory.reconstruct(to_refresh_token_record(model))

    async def add(self, token: RefreshToken) -> None:
        """Stage a newly issued token for insertion."""
        record = self._factory.deconstruct(token)
        self._session.add(RefreshTokenModel(**to_refresh_token_model_kwargs(record)))

    async def mark_replaced(self, token: RefreshToken) -> bool:
        """Close the predecessor half of a rotation, reporting whether this call won.

        Returns
        -------
        bool
            ``True`` if this call closed the token. ``False`` if it was already
            closed, which means a concurrent request rotated it first.

        Notes
        -----
        The ``WHERE replaced_by IS NULL`` predicate is the concurrency control,
        and it is why this aggregate needs no version column. Exactly one of two
        simultaneous rotations affects a row.

        The loser is told, not raised at. It presented a token that was genuinely
        current when it read it, so this is not reuse and must not revoke a
        lineage -- treating an ordinary double-click as theft would log people
        out for using two tabs.
        """
        record = self._factory.deconstruct(token)
        statement = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.id == record.id,
                RefreshTokenModel.replaced_by.is_(None),
            )
            .values(replaced_by=record.replaced_by)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return result.rowcount == 1

    async def revoke_lineage(self, lineage_id: UUID, *, at: datetime) -> int:
        """Revoke every unrevoked token in one rotation chain.

        Returns
        -------
        int
            How many tokens this revoked. Zero means the lineage was already
            dead, which is what makes repeated reuse detection cheap: an attacker
            hammering a stolen token triggers one real revocation and then a
            series of no-ops.

        Notes
        -----
        One statement against one indexed column. The alternative -- walking
        ``parent_token_id`` up to the root and back down -- is slowest for the
        longest chain, which is both the most valuable to a thief and the one
        most likely to be under active abuse at the moment this runs.

        ``revoked_at IS NULL`` in the predicate preserves the first revocation
        instant, matching :meth:`RefreshToken.revoked`. The instant a lineage was
        killed is what an incident review reads, and overwriting it would move
        the evidence.
        """
        statement = (
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.lineage_id == lineage_id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return result.rowcount
