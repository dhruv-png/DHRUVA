"""Enrol a broker application, establish a session, and report where we stand.

Three use cases over the credential store ADR-077 built. None of them knows
which broker it is talking to: the vendor lives behind
:class:`~dhruva.contexts.platform.domain.broker.ports.BrokerAuthenticator`, and
the two payload shapes are generic domain documents.

The plaintext discipline is worth stating once, because it is the whole point of
the arrangement. A secret exists in memory at exactly three moments: when the
operator types it and it is sealed; when an enrolment credential is opened to
sign a handshake; and when a session token is opened by something about to sign
a request. Nothing here returns a plaintext to its caller, and the result types
carry none -- which is why the commands can print their results without a
redaction filter having to save them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.platform.application.identity.broker_credentials import (
    GetBrokerCredential,
    StoreBrokerCredential,
    StoreBrokerCredentialCommand,
)
from dhruva.contexts.platform.domain.broker.session import (
    BrokerAuthState,
    BrokerSession,
    parse_broker_application,
    parse_broker_session,
    serialise_broker_application,
    serialise_broker_session,
)
from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
from dhruva.shared.identity import CredentialId

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from dhruva.contexts.platform.domain.broker.ports import (
        BrokerAuthenticator,
    )
    from dhruva.contexts.platform.domain.broker.session import BrokerApplication
    from dhruva.contexts.platform.domain.identity.keys import KeyProvider
    from dhruva.contexts.platform.domain.identity.ports import (
        CredentialOpener,
        CredentialSealer,
        IdentityUnitOfWork,
    )
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId
    from dhruva.shared.time.clock import Clock

__all__ = [
    "BrokerAuthenticationStatus",
    "DescribeBrokerAuthentication",
    "EnrolBrokerApplication",
    "EnrolledApplication",
    "EstablishBrokerSession",
    "EstablishedSession",
]


@dataclass(frozen=True, slots=True)
class EnrolledApplication:
    """What an operator is told after enrolling. No secret, by construction."""

    account_id: AccountId
    broker: str
    rotated: bool
    stored_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class EstablishedSession:
    """What an operator is told after logging in.

    Carries the broker's user id, which is the one fact that proves the session
    belongs to the account the operator meant. It carries no token: a command
    that printed one would put it in a scrollback, and a result object that held
    one would eventually be logged by something that did not know.
    """

    account_id: AccountId
    broker: str
    broker_user_id: str
    issued_at: datetime
    expires_at: datetime
    replaced_previous: bool


@dataclass(frozen=True, slots=True)
class BrokerAuthenticationStatus:
    """Where an account stands with a broker, without opening anything.

    ``expires_at`` is present only when a session exists, and it is the reason
    this query opens the session credential at all: expiry lives inside the
    sealed document, so "is my login still good" cannot be answered from
    metadata. The enrolment credential is never opened here -- knowing whether
    one exists needs no plaintext.
    """

    account_id: AccountId
    broker: str
    state: BrokerAuthState
    broker_user_id: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    enrolment_rotated_at: datetime | None = None


class EnrolBrokerApplication:
    """Seal the owner's long-lived broker application credential.

    First enrolment and rotation are one operation, following
    :class:`StoreBrokerCredential`: an operator replacing a rotated secret is
    doing the same thing as one entering it for the first time, and a separate
    entry point would differ only in which error it raised when its assumption
    about existence was wrong.
    """

    __slots__ = ("_read", "_store")

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
        key_provider: KeyProvider,
        seal: CredentialSealer,
    ) -> None:
        """Bind to a transaction factory, a key provider and a sealer."""
        self._store = StoreBrokerCredential(unit_of_work_factory, key_provider, seal)
        self._read = GetBrokerCredential(unit_of_work_factory)

    async def execute(
        self,
        account_id: AccountId,
        broker: str,
        application: BrokerApplication,
        *,
        at: datetime,
    ) -> EnrolledApplication:
        """Store the application credential, replacing only the enrolment row."""
        existing = await self._read.summary(account_id, broker, CredentialPurpose.ENROLMENT)
        stored = await self._store.execute(
            StoreBrokerCredentialCommand(
                account_id=account_id,
                broker=broker,
                purpose=CredentialPurpose.ENROLMENT,
                secret=serialise_broker_application(application),
                at=at,
                credential_id=CredentialId.new(),
            )
        )
        return EnrolledApplication(
            account_id=account_id,
            broker=broker,
            rotated=existing is not None,
            stored_at=at,
            version=stored.version,
        )


class EstablishBrokerSession:
    """Exchange a request token for a session and seal the result.

    The enrolment credential is opened here and nowhere else in the flow, for
    the duration of one handshake. That is the narrowest this can be: the
    broker's signature is computed from the application secret, so something has
    to hold it, and the alternative -- passing the sealed credential into the
    adapter -- would put decryption inside the HTTP layer.
    """

    __slots__ = ("_authenticator", "_clock", "_key_provider", "_open", "_read", "_store")

    def __init__(  # noqa: PLR0913, PLR0917 - distinct collaborator capabilities
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
        key_provider: KeyProvider,
        seal: CredentialSealer,
        open_credential: CredentialOpener,
        authenticator: BrokerAuthenticator,
        clock: Clock,
    ) -> None:
        """Bind the login to its transaction, cipher, broker and clock."""
        self._store = StoreBrokerCredential(unit_of_work_factory, key_provider, seal)
        self._read = GetBrokerCredential(unit_of_work_factory)
        self._key_provider = key_provider
        self._open = open_credential
        self._authenticator = authenticator
        self._clock = clock

    async def login_url(self, account_id: AccountId, broker: str) -> str:
        """Return the broker's login page for the enrolled application.

        Raises
        ------
        NotFoundError
            If nothing is enrolled. The operator's next step is enrolment, not
            a login, and saying so is more useful than an empty URL.
        """
        application = await self._enrolled_application(account_id, broker)
        return self._authenticator.login_url(application)

    async def execute(
        self,
        account_id: AccountId,
        broker: str,
        request_token: SecretValue,
    ) -> EstablishedSession:
        """Complete the handshake and store the session under its own purpose.

        Raises
        ------
        NotFoundError
            If no application credential is enrolled.
        UpstreamAuthenticationError
            If the broker rejected the request token or the signature.
        """
        application = await self._enrolled_application(account_id, broker)
        previous = await self._read.summary(account_id, broker, CredentialPurpose.SESSION)

        material = await self._authenticator.exchange(application, request_token)
        session = BrokerSession(
            token=material.token,
            broker_user_id=material.broker_user_id,
            issued_at=material.issued_at,
            expires_at=material.expires_at,
        )

        await self._store.execute(
            StoreBrokerCredentialCommand(
                account_id=account_id,
                broker=broker,
                purpose=CredentialPurpose.SESSION,
                secret=serialise_broker_session(session),
                at=self._clock.now(),
                credential_id=CredentialId.new(),
            )
        )
        return EstablishedSession(
            account_id=account_id,
            broker=broker,
            broker_user_id=session.broker_user_id,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            replaced_previous=previous is not None,
        )

    async def _enrolled_application(self, account_id: AccountId, broker: str) -> BrokerApplication:
        credential = await self._read.sealed(account_id, broker, CredentialPurpose.ENROLMENT)
        return parse_broker_application(self._open(credential, self._key_provider))


class DescribeBrokerAuthentication:
    """Answer "can I fetch market data yet, and if not why not".

    Read-only and side-effect free. It does not refresh, does not repair and
    does not delete an expired session: an operator who runs a status command
    and finds their state changed by it has been given a worse tool than none.
    """

    __slots__ = ("_clock", "_key_provider", "_open", "_read")

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IdentityUnitOfWork],
        key_provider: KeyProvider,
        open_credential: CredentialOpener,
        clock: Clock,
    ) -> None:
        """Bind the query to its transaction, cipher and clock."""
        self._read = GetBrokerCredential(unit_of_work_factory)
        self._key_provider = key_provider
        self._open = open_credential
        self._clock = clock

    async def execute(self, account_id: AccountId, broker: str) -> BrokerAuthenticationStatus:
        """Report the state without changing it."""
        enrolment = await self._read.summary(account_id, broker, CredentialPurpose.ENROLMENT)
        if enrolment is None:
            return BrokerAuthenticationStatus(
                account_id=account_id,
                broker=broker,
                state=BrokerAuthState.ENROLMENT_MISSING,
            )

        stored_session = await self._read.summary(account_id, broker, CredentialPurpose.SESSION)
        if stored_session is None:
            return BrokerAuthenticationStatus(
                account_id=account_id,
                broker=broker,
                state=BrokerAuthState.SESSION_MISSING,
                enrolment_rotated_at=enrolment.rotated_at,
            )

        credential = await self._read.sealed(account_id, broker, CredentialPurpose.SESSION)
        session = parse_broker_session(self._open(credential, self._key_provider))
        expired = session.is_expired(self._clock)
        return BrokerAuthenticationStatus(
            account_id=account_id,
            broker=broker,
            state=BrokerAuthState.SESSION_EXPIRED if expired else BrokerAuthState.SUCCESS,
            broker_user_id=session.broker_user_id,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            enrolment_rotated_at=enrolment.rotated_at,
        )
