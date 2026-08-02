"""Authenticating a principal (ADR-072, ADR-071, ADR-053).

The one thing to understand before reading the code
---------------------------------------------------
**A failed authentication commits.** Not "commits the audit row and rolls back
the rest" -- there is no rest. The transaction writes one audit record and its
outbox event, commits, and only then does the use case raise.

That inverts the usual reading of ADR-053, where any exception rolls everything
back, and it is deliberate rather than an exception being smuggled in. Plan §15.1
requires an audit record for **every** authentication, and §15.1's own reasoning
says why the failures are the ones that matter: a run of failed logins is the
signal, and it exists only if the failures were written down. Rolling back on
failure would mean the audit log recorded successes only -- which is the log that
cannot show a brute-force attempt, the single most likely thing anybody will read
it for.

So the ordering is: decide, record, commit, raise. The raise happens outside the
transaction block, after the commit has returned, so there is no path on which
the caller sees an error for work that was not durably recorded.

Why every failure produces the same error
-----------------------------------------
An unknown subject, a wrong password and a disabled principal all raise
:class:`~dhruva.shared.errors.AuthenticationError` with the same message. ADR-072
requires it: an error distinguishing them is a user-enumeration oracle, useful to
an attacker and to nobody else. The distinction is recorded in the audit record's
``subject`` field, which is where it is useful.

The same reasoning is why an unknown subject still pays for a password
verification. Identical errors returned in visibly different times are the same
oracle with extra steps.
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
from dhruva.shared.errors import AuthenticationError
from dhruva.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from uuid import UUID

    from dhruva.contexts.platform.application.identity.sessions import SessionPolicy
    from dhruva.contexts.platform.domain.identity.passwords import PasswordHasher
    from dhruva.contexts.platform.domain.identity.ports import (
        IdentityUnitOfWork,
        RefreshTokenMinter,
    )
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.domain.identity.tokens import TokenIssuer
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId
    from dhruva.shared.time import Clock

__all__ = ["AuthenticateUseCase", "IssuedSession"]

_log = get_logger(__name__)

#: What the caller is told, for every way this can fail. One message, one code.
_REFUSED = "authentication failed"

#: Stands in for a subject that was submitted blank.
#:
#: ``AuditRecord`` refuses a blank actor, and rightly -- a record naming nobody
#: is not evidence of anything. But an empty login form is an ordinary event that
#: still has to be auditable, and letting the record's own invariant fire here
#: would roll the transaction back and lose the very record the attempt should
#: have produced. So the blank is named rather than passed through.
_BLANK_SUBJECT = "(blank)"


class AuthenticateUseCase:
    """Exchanges a subject and a password for a session.

    Every dependency is a protocol, so nothing here imports SQLAlchemy, PyJWT or
    argon2 -- and the whole use case runs against fakes in the unit suite while
    the identical code runs against PostgreSQL in the integration suite.
    """

    __slots__ = ("_clock", "_hasher", "_minter", "_policy", "_tokens", "_unit_of_work_factory")

    def __init__(
        self,
        unit_of_work_factory: Callable[[], IdentityUnitOfWork],
        *,
        hasher: PasswordHasher,
        tokens: TokenIssuer,
        minter: RefreshTokenMinter,
        policy: SessionPolicy,
        clock: Clock,
    ) -> None:
        """Wire the use case to its ports.

        A *factory* rather than a Unit of Work, because one use case is one
        transaction (ADR-053) and an instance handed in would be reused across
        calls -- which is the ambient-transaction problem, one layer up.
        """
        self._unit_of_work_factory = unit_of_work_factory
        self._hasher = hasher
        self._tokens = tokens
        self._minter = minter
        self._policy = policy
        self._clock = clock

    async def execute(
        self, *, subject: str, password: SecretValue, correlation_id: UUID
    ) -> IssuedSession:
        """Authenticate ``subject`` and issue a session.

        Returns
        -------
        IssuedSession
            The access token, the refresh token secret, and when the first
            expires.

        Raises
        ------
        AuthenticationError
            If the subject is unknown, the principal is disabled, or the password
            does not verify. The three are indistinguishable to the caller and
            distinguished in the audit log.
        """
        now = self._clock.now()

        async with self._unit_of_work_factory() as uow:
            principal = await uow.principals.get_by_subject(subject)
            refusal = self._refusal_reason(principal, password)

            if refusal is not None or principal is None:
                await self._record(
                    uow,
                    actor=subject.strip() or _BLANK_SUBJECT,
                    subject=refusal or "unknown-principal",
                    outcome=AuditOutcome.FAILED,
                    account_id=principal.account_id if principal else UNATTRIBUTED_ACCOUNT,
                    correlation_id=correlation_id,
                    now=now,
                )
                # Commit *before* raising. The audit record is the deliverable of
                # a failed authentication; rolling it back would leave the log
                # showing successes only.
                await uow.commit()
                _log.info(
                    "authentication refused",
                    reason=refusal,
                    correlation_id=str(correlation_id),
                )
                raise AuthenticationError(_REFUSED)

            prepared = prepare_session(
                principal,
                tokens=self._tokens,
                minter=self._minter,
                policy=self._policy,
                now=now,
            )
            await uow.refresh_tokens.add(prepared.refresh)
            await self._record(
                uow,
                actor=principal.subject,
                subject="session",
                outcome=AuditOutcome.SUCCEEDED,
                account_id=principal.account_id,
                correlation_id=correlation_id,
                now=now,
            )
            await uow.commit()
            return prepared.session

    def _refusal_reason(self, principal: Principal | None, password: SecretValue) -> str | None:
        """Return why authentication fails, or ``None`` if it succeeds.

        The string is for the **audit log**, never for the caller. It is what
        makes the log able to distinguish a brute-force attempt against one
        subject from a scan across many, which the uniform error deliberately
        cannot.

        Every path spends the cost of a password verification, including the two
        that already know the answer. See the module docstring: identical errors
        returned in visibly different times are a user-enumeration oracle with
        extra steps.
        """
        if principal is None:
            self._hasher.verify_nothing(password)
            return "unknown-subject"
        if not principal.is_active:
            self._hasher.verify_nothing(password)
            return "disabled-principal"
        if not self._hasher.verify(password, principal.password_hash):
            return "wrong-password"
        return None

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

        ``occurred_at`` and ``recorded_at`` are the same instant here, because
        the platform learns of an authentication attempt by handling it. They
        stay separate fields because a replayed or deferred write would make them
        differ, and a reader needs to know which question they are asking.
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
