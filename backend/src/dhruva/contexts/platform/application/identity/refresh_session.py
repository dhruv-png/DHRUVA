"""Rotating a session, and treating a second presentation as theft (ADR-072).

The decision this use case exists to make
-----------------------------------------
A refresh token that arrives already spent has one benign explanation and one
serious one, and they are indistinguishable at the moment of presentation. ADR-072
picks the serious reading and ADR-022 says why: revoking the lineage logs out an
honest user occasionally and stops a thief every time.

The *rule* is not here -- it is :meth:`RefreshToken.verdict`, in the domain, where
it can be enumerated without a database. What is here is the consequence of each
verdict, which is the part that touches state.

What the caller learns, and what it does not
--------------------------------------------
Reuse and ordinary revocation both raise
:class:`~dhruva.shared.errors.TokenRevokedError`, with the same message. Telling a
caller "this was revoked because we detected you reusing it" tells a thief they
have been noticed, which is the one thing worth withholding at that moment. The
audit log distinguishes them; the response does not.

Expiry is different and does get its own error, because the client's recovery
genuinely differs: an expired token means log in again, and a client that could
not tell would have to treat every ordinary session end as a possible compromise.

Every path commits
------------------
Same inversion as the authentication use case, for the same reason: the audit
record is the deliverable of a failed refresh, and a reuse detection that rolled
back would revoke the lineage in memory and leave no evidence it had. Decide,
record, commit, raise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.application.identity.sessions import IssuedSession, prepare_session
from dhruva.contexts.platform.domain.audit import (
    UNATTRIBUTED_ACCOUNT,
    AuditAction,
    AuditOutcome,
    AuditRecord,
)
from dhruva.contexts.platform.domain.identity.refresh import RefreshVerdict
from dhruva.shared.errors import (
    AuthenticationError,
    InvariantViolation,
    TokenExpiredError,
    TokenRevokedError,
)
from dhruva.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from dhruva.contexts.platform.application.identity.sessions import SessionPolicy
    from dhruva.contexts.platform.domain.identity.ports import (
        IdentityUnitOfWork,
        RefreshTokenMinter,
    )
    from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
    from dhruva.contexts.platform.domain.identity.tokens import TokenIssuer
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId
    from dhruva.shared.time import Clock

__all__ = ["RefreshSessionUseCase"]

_log = get_logger(__name__)

_REFUSED = "refresh failed"
_REVOKED = "refresh token is no longer valid"
_EXPIRED = "refresh token has expired"


class RefreshSessionUseCase:
    """Exchanges a refresh token for a new session, or detects that it was stolen."""

    __slots__ = ("_clock", "_minter", "_policy", "_tokens", "_unit_of_work_factory")

    def __init__(
        self,
        unit_of_work_factory: Callable[[], IdentityUnitOfWork],
        *,
        tokens: TokenIssuer,
        minter: RefreshTokenMinter,
        policy: SessionPolicy,
        clock: Clock,
    ) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work_factory = unit_of_work_factory
        self._tokens = tokens
        self._minter = minter
        self._policy = policy
        self._clock = clock

    async def execute(self, *, presented: SecretValue, correlation_id: UUID) -> IssuedSession:
        """Rotate the presented refresh token and issue a new session.

        Raises
        ------
        TokenRevokedError
            If the token was revoked, or if presenting it detected reuse and
            revoked its lineage. Deliberately the same error for both.
        TokenExpiredError
            If the token passed its lifetime.
        AuthenticationError
            If the token matches nothing, if its principal no longer exists or is
            disabled, or if a concurrent request rotated it first.
        """
        now = self._clock.now()
        digest = self._minter.digest(presented)

        async with self._unit_of_work_factory() as uow:
            token = await uow.refresh_tokens.get_by_hash(digest)

            if token is None:
                # A token this platform never issued, or one from a database
                # since rebuilt. There is no principal and no tenant to attribute
                # it to, which is what the reserved account is for.
                await self._record(
                    uow,
                    actor="unknown-token",
                    subject="unknown-token",
                    outcome=AuditOutcome.FAILED,
                    account_id=UNATTRIBUTED_ACCOUNT,
                    correlation_id=correlation_id,
                    now=now,
                )
                await uow.commit()
                raise AuthenticationError(_REFUSED)

            verdict = token.verdict(now)
            if verdict is not RefreshVerdict.USABLE:
                await self._refuse(
                    uow, token=token, verdict=verdict, correlation_id=correlation_id, now=now
                )

            principal = await uow.principals.get(token.principal_id)
            if principal is None or not principal.is_active:
                # The session outlived the principal. Kill the lineage as well as
                # refusing: a disabled operator's outstanding refresh tokens are
                # exactly what ADR-072's consequences section says must be
                # revoked, and doing it here means it happens even if whoever
                # disabled the principal forgot.
                await uow.refresh_tokens.revoke_lineage(token.lineage_id, at=now)
                await self._record(
                    uow,
                    actor=str(token.principal_id),
                    subject="principal-unavailable",
                    outcome=AuditOutcome.FAILED,
                    account_id=token.account_id,
                    correlation_id=correlation_id,
                    now=now,
                )
                await uow.commit()
                raise AuthenticationError(_REFUSED)

            prepared = prepare_session(
                principal,
                tokens=self._tokens,
                minter=self._minter,
                policy=self._policy,
                now=now,
                predecessor=token,
            )
            # `prepare_session` returns the closed half whenever it is given a
            # predecessor, so this is narrowing rather than a real check. It
            # raises rather than asserting because `assert` is stripped under
            # `-O`, and a silent `None` here would stage a successor while
            # leaving the old token open -- the state that makes a legitimately
            # rotated token look reusable forever.
            predecessor = prepared.predecessor
            if predecessor is None:  # pragma: no cover - unreachable on this path
                msg = "a rotation was prepared without closing its predecessor"
                raise InvariantViolation(msg)

            # Close the old token first. Its conditional update is what decides
            # whether this rotation happened at all, so staging the successor
            # before knowing the answer would leave an orphan on the losing path.
            if not await uow.refresh_tokens.mark_replaced(predecessor):
                # Another request rotated this token between our read and our
                # write. That is a double-click, not a theft: the token was
                # genuinely current when we read it, so the lineage must survive.
                await self._record(
                    uow,
                    actor=principal.subject,
                    subject="rotation-lost-race",
                    outcome=AuditOutcome.FAILED,
                    account_id=principal.account_id,
                    correlation_id=correlation_id,
                    now=now,
                )
                await uow.commit()
                raise AuthenticationError(_REFUSED)

            await uow.refresh_tokens.add(prepared.refresh)
            await self._record(
                uow,
                actor=principal.subject,
                subject="session-refresh",
                outcome=AuditOutcome.SUCCEEDED,
                account_id=principal.account_id,
                correlation_id=correlation_id,
                now=now,
            )
            await uow.commit()
            return prepared.session

    async def _refuse(
        self,
        uow: IdentityUnitOfWork,
        *,
        token: RefreshToken,
        verdict: RefreshVerdict,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        """Record an unusable presentation, commit it, and raise.

        Never returns. Typed as ``None`` rather than ``NoReturn`` because the
        raise happens on every branch of the match and mypy can see it, while
        ``NoReturn`` on an ``async def`` reads as a coroutine that never
        completes.
        """
        if verdict is RefreshVerdict.REUSED:
            revoked = await uow.refresh_tokens.revoke_lineage(token.lineage_id, at=now)
            _log.warning(
                "refresh token reuse detected; lineage revoked",
                lineage_id=str(token.lineage_id),
                tokens_revoked=revoked,
                correlation_id=str(correlation_id),
            )

        await self._record(
            uow,
            actor=str(token.principal_id),
            subject=f"refresh-{verdict.value}",
            outcome=AuditOutcome.FAILED,
            account_id=token.account_id,
            correlation_id=correlation_id,
            now=now,
        )
        await uow.commit()

        if verdict is RefreshVerdict.EXPIRED:
            raise TokenExpiredError(_EXPIRED)
        # Reuse and ordinary revocation, deliberately indistinguishable.
        raise TokenRevokedError(_REVOKED)

    @staticmethod
    async def _record(
        uow: IdentityUnitOfWork,
        *,
        actor: str,
        subject: str,
        outcome: AuditOutcome,
        account_id: AccountId,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        """Write one authentication audit record into this transaction.

        A refresh is an authentication: it establishes who the caller is, from a
        credential they present. Recording it under a different action would put
        half the platform's authentications outside the query plan §15.1 asks
        for.
        """
        await uow.audit.record(
            AuditRecord(
                actor=actor,
                action=AuditAction.AUTHENTICATION,
                subject=subject,
                outcome=outcome,
                occurred_at=now,
                recorded_at=now,
                account_id=account_id,
                correlation_id=correlation_id,
            )
        )
