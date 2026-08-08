"""Enrolment, login and status, over the real cipher and a fake broker.

The credential store, the sealer and the opener are the production ones; only
the broker's HTTP side is a double. That is deliberate: the claims worth making
here are cryptographic and lifecycle claims -- that a session write cannot reach
an enrolment credential, that a stored token is not recoverable without the
master key, that a credential cannot be read across accounts, brokers or
purposes -- and a fake cipher would let every one of them pass while being false.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from dhruva.contexts.platform.application.broker import (
    DescribeBrokerAuthentication,
    EnrolBrokerApplication,
    EstablishBrokerSession,
)
from dhruva.contexts.platform.domain.broker.ports import BrokerSessionMaterial
from dhruva.contexts.platform.domain.broker.session import (
    BrokerApplication,
    BrokerAuthState,
    parse_broker_application,
    parse_broker_session,
)
from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
from dhruva.contexts.platform.infrastructure.crypto import (
    MASTER_KEY_BYTES,
    MasterKeyProvider,
    open_credential,
    seal_credential,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import NotFoundError, SafetyError, UpstreamAuthenticationError
from dhruva.shared.identity import AccountId
from dhruva.shared.time.clock import FrozenClock
from tests.unit.identity.fakes import FakeUnitOfWork

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

BROKER: Final = "zerodha"
OTHER_BROKER: Final = "another-broker"
ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER_ACCOUNT: Final = AccountId.deterministic("someone-else")

#: Obviously synthetic. Nothing here is or resembles real broker material.
IDENTIFIER: Final = "synthetic-api-identifier"
APPLICATION_MATERIAL: Final = "synthetic-application-material"
REQUEST_MATERIAL: Final = "synthetic-request-material"
SESSION_MATERIAL: Final = "synthetic-session-material"

ISSUED: Final = datetime(2026, 8, 8, 4, 30, tzinfo=UTC)
EXPIRES: Final = datetime(2026, 8, 9, 0, 30, tzinfo=UTC)


class FakeBroker:
    """A broker that answers the handshake, or refuses it.

    Records what it was handed, because the one thing a fake can prove that the
    real adapter cannot is *which* credential the use case decided to sign with
    -- a login that reached for the session credential instead of the enrolment
    one would still produce a plausible-looking result.
    """

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, str, str]] = []
        self.issues = SESSION_MATERIAL

    def login_url(self, application: BrokerApplication) -> str:
        """Return a login URL carrying only the public half."""
        return f"https://broker.invalid/login?api_key={application.identifier.reveal()}"

    async def exchange(
        self, application: BrokerApplication, request_token: SecretValue
    ) -> BrokerSessionMaterial:
        """Record the handshake and answer it."""
        self.calls.append(
            (
                application.identifier.reveal(),
                application.secret.reveal(),
                request_token.reveal(),
            )
        )
        if self.refuse:
            message = "the broker rejected the login"
            raise UpstreamAuthenticationError(message, provider=BROKER)
        return BrokerSessionMaterial(
            token=SecretValue(self.issues, register=False),
            broker_user_id="XX0000",
            issued_at=ISSUED,
            expires_at=EXPIRES,
        )


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return a provider over a random master key, new for every test."""
    key = SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)
    return MasterKeyProvider(key)


@pytest.fixture
def unit_of_work() -> FakeUnitOfWork:
    """Return the one transaction every use case in a test shares."""
    return FakeUnitOfWork()


@pytest.fixture
def factory(unit_of_work: FakeUnitOfWork) -> Callable[[AccountId], FakeUnitOfWork]:
    """Return a factory handing out that transaction, so state persists."""
    return lambda _account_id: unit_of_work


@pytest.fixture
def clock() -> FrozenClock:
    """Return a clock inside the session's lifetime."""
    return FrozenClock(ISSUED + timedelta(hours=1))


@pytest.fixture
def enrol(
    factory: Callable[[AccountId], FakeUnitOfWork], provider: MasterKeyProvider
) -> EnrolBrokerApplication:
    """Return the enrolment use case over the real sealer."""
    return EnrolBrokerApplication(factory, provider, seal_credential)


@pytest.fixture
def broker() -> FakeBroker:
    """Return a broker that accepts the handshake."""
    return FakeBroker()


@pytest.fixture
def login(
    factory: Callable[[AccountId], FakeUnitOfWork],
    provider: MasterKeyProvider,
    broker: FakeBroker,
    clock: FrozenClock,
) -> EstablishBrokerSession:
    """Return the login use case over the real cipher and a fake broker."""
    return EstablishBrokerSession(
        factory, provider, seal_credential, open_credential, broker, clock
    )


@pytest.fixture
def status(
    factory: Callable[[AccountId], FakeUnitOfWork],
    provider: MasterKeyProvider,
    clock: FrozenClock,
) -> DescribeBrokerAuthentication:
    """Return the read-only status query."""
    return DescribeBrokerAuthentication(factory, provider, open_credential, clock)


def _application(secret: str = APPLICATION_MATERIAL) -> BrokerApplication:
    return BrokerApplication(
        identifier=SecretValue(IDENTIFIER, register=False),
        secret=SecretValue(secret, register=False),
    )


async def _enrol_and_login(enrol: EnrolBrokerApplication, login: EstablishBrokerSession) -> None:
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)
    await login.execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))


# --------------------------------------------------------------------------- #
# Enrolment
# --------------------------------------------------------------------------- #


async def test_an_enrolled_application_is_stored_sealed_and_reopens(
    enrol: EnrolBrokerApplication,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """Both halves survive the seal, and neither is in the stored row."""
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    stored = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    assert stored is not None
    assert APPLICATION_MATERIAL.encode() not in stored.secret.ciphertext
    application = parse_broker_application(open_credential(stored, provider))
    assert application.identifier.reveal() == IDENTIFIER
    assert application.secret.reveal() == APPLICATION_MATERIAL


async def test_enrolling_again_rotates_and_reports_that_it_did(
    enrol: EnrolBrokerApplication,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """An operator rotating a secret is told which of the two things happened."""
    first = await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)
    second = await enrol.execute(
        ACCOUNT,
        BROKER,
        _application("synthetic-application-material-rotated"),
        at=ISSUED + timedelta(days=30),
    )

    assert first.rotated is False
    assert second.rotated is True
    assert second.version == first.version + 1
    stored = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    assert stored is not None
    assert (
        parse_broker_application(open_credential(stored, provider)).secret.reveal()
        == "synthetic-application-material-rotated"
    )


async def test_rotating_the_application_leaves_the_session_alone(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """A rotated API secret must not silently invalidate a live login.

    The operator would see broker calls failing right after a routine rotation
    and conclude the rotation broke the account, which is a bad afternoon spent
    on the wrong problem.
    """
    await _enrol_and_login(enrol, login)
    before = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)

    await enrol.execute(
        ACCOUNT,
        BROKER,
        _application("synthetic-application-material-rotated"),
        at=ISSUED + timedelta(days=30),
    )

    after = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert after == before
    assert after is not None
    assert parse_broker_session(open_credential(after, provider)).token.reveal() == SESSION_MATERIAL


async def test_an_enrolment_result_carries_no_secret(enrol: EnrolBrokerApplication) -> None:
    """It is printed to a terminal, so it must be safe to print."""
    result = await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    rendered = repr(result)
    assert APPLICATION_MATERIAL not in rendered
    assert IDENTIFIER not in rendered


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


async def test_logging_in_without_enrolling_says_so(login: EstablishBrokerSession) -> None:
    """The operator's next step is enrolment, and the error names it."""
    with pytest.raises(NotFoundError):
        await login.execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))
    with pytest.raises(NotFoundError):
        await login.login_url(ACCOUNT, BROKER)


async def test_the_login_signs_with_the_enrolled_application(
    enrol: EnrolBrokerApplication, login: EstablishBrokerSession, broker: FakeBroker
) -> None:
    """The handshake receives the enrolment credential, opened, and nothing else."""
    await _enrol_and_login(enrol, login)

    assert broker.calls == [(IDENTIFIER, APPLICATION_MATERIAL, REQUEST_MATERIAL)]


async def test_a_session_is_stored_under_its_own_purpose(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """ADR-077 in use: two credentials, one broker, one account."""
    await _enrol_and_login(enrol, login)

    enrolment = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    session = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert enrolment is not None
    assert session is not None
    assert enrolment.credential_id != session.credential_id
    assert parse_broker_session(open_credential(session, provider)).expires_at == EXPIRES


async def test_logging_in_again_replaces_only_the_session(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    broker: FakeBroker,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """The daily act. It must not touch the credential it is signed with."""
    await _enrol_and_login(enrol, login)
    enrolment_before = await unit_of_work.credentials.get(
        ACCOUNT, BROKER, CredentialPurpose.ENROLMENT
    )

    broker.issues = "synthetic-session-material-day-two"
    result = await login.execute(
        ACCOUNT, BROKER, SecretValue("synthetic-request-material-two", register=False)
    )

    assert result.replaced_previous is True
    enrolment_after = await unit_of_work.credentials.get(
        ACCOUNT, BROKER, CredentialPurpose.ENROLMENT
    )
    assert enrolment_after == enrolment_before
    session = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert session is not None
    assert session.version == 2
    assert (
        parse_broker_session(open_credential(session, provider)).token.reveal()
        == "synthetic-session-material-day-two"
    )


async def test_a_refused_login_stores_nothing(
    factory: Callable[[AccountId], FakeUnitOfWork],
    enrol: EnrolBrokerApplication,
    provider: MasterKeyProvider,
    unit_of_work: FakeUnitOfWork,
    clock: FrozenClock,
) -> None:
    """A rejected handshake must not leave a half-session behind.

    A stored credential that never worked is worse than none: the status command
    would report SUCCESS and the operator would look everywhere except at the
    login.
    """
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)
    refusing = EstablishBrokerSession(
        factory, provider, seal_credential, open_credential, FakeBroker(refuse=True), clock
    )

    with pytest.raises(UpstreamAuthenticationError):
        await refusing.execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))

    assert await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION) is None


async def test_a_login_result_carries_no_token(
    enrol: EnrolBrokerApplication, login: EstablishBrokerSession
) -> None:
    """It is printed. The broker user id is there; the access token is not."""
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    result = await login.execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))

    rendered = repr(result)
    assert result.broker_user_id == "XX0000"
    assert SESSION_MATERIAL not in rendered
    assert REQUEST_MATERIAL not in rendered
    assert APPLICATION_MATERIAL not in rendered


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


async def test_another_account_is_not_authenticated_by_this_one(
    enrol: EnrolBrokerApplication, status: DescribeBrokerAuthentication
) -> None:
    """ADR-004. One household member's login is not another's."""
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    assert (await status.execute(OTHER_ACCOUNT, BROKER)).state is BrokerAuthState.ENROLMENT_MISSING


async def test_another_broker_is_not_authenticated_by_this_one(
    enrol: EnrolBrokerApplication, status: DescribeBrokerAuthentication
) -> None:
    """The broker stays part of the key; purpose was added beside it, not over it."""
    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    assert (await status.execute(ACCOUNT, OTHER_BROKER)).state is BrokerAuthState.ENROLMENT_MISSING


async def test_a_session_ciphertext_cannot_be_read_as_an_enrolment_credential(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    unit_of_work: FakeUnitOfWork,
    provider: MasterKeyProvider,
) -> None:
    """ADR-077's binding, exercised through the real flow rather than in isolation.

    The two credentials now hold genuinely different documents, so without the
    binding a transplant would not merely decrypt -- it would decrypt into a
    session payload that the enrolment parser would reject with a confusing
    error. The cipher refusing first is what makes the failure legible.
    """
    await _enrol_and_login(enrol, login)
    enrolment = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    session = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert enrolment is not None
    assert session is not None

    import dataclasses  # noqa: PLC0415 - test-only import

    impostor = dataclasses.replace(enrolment, secret=session.secret)
    with pytest.raises(SafetyError):
        open_credential(impostor, provider)


async def test_a_stored_session_is_unreadable_under_another_master_key(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    unit_of_work: FakeUnitOfWork,
) -> None:
    """The token at rest is worth nothing without the key from process config."""
    await _enrol_and_login(enrol, login)
    session = await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert session is not None
    stranger = MasterKeyProvider(
        SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)
    )

    with pytest.raises(SafetyError):
        open_credential(session, stranger)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


async def test_status_distinguishes_the_two_kinds_of_missing(
    enrol: EnrolBrokerApplication, status: DescribeBrokerAuthentication
) -> None:
    """Never-enrolled and never-logged-in need different next actions."""
    assert (await status.execute(ACCOUNT, BROKER)).state is BrokerAuthState.ENROLMENT_MISSING

    await enrol.execute(ACCOUNT, BROKER, _application(), at=ISSUED)

    assert (await status.execute(ACCOUNT, BROKER)).state is BrokerAuthState.SESSION_MISSING


async def test_status_reports_success_while_the_session_lives(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    status: DescribeBrokerAuthentication,
) -> None:
    """And carries the facts that let an operator check it is the right account."""
    await _enrol_and_login(enrol, login)

    reported = await status.execute(ACCOUNT, BROKER)

    assert reported.state is BrokerAuthState.SUCCESS
    assert reported.broker_user_id == "XX0000"
    assert reported.expires_at == EXPIRES


async def test_status_reports_expiry_rather_than_absence(
    factory: Callable[[AccountId], FakeUnitOfWork],
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    provider: MasterKeyProvider,
) -> None:
    """An expired session is still stored, and saying so is the useful answer.

    Reporting SESSION_MISSING instead would send an operator looking for a
    write that failed, when what actually happened is that the morning came.
    """
    await _enrol_and_login(enrol, login)
    later = DescribeBrokerAuthentication(
        factory, provider, open_credential, FrozenClock(EXPIRES + timedelta(minutes=1))
    )

    reported = await later.execute(ACCOUNT, BROKER)

    assert reported.state is BrokerAuthState.SESSION_EXPIRED
    assert reported.expires_at == EXPIRES


async def test_status_changes_nothing(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    status: DescribeBrokerAuthentication,
    unit_of_work: FakeUnitOfWork,
) -> None:
    """A read-only command that repaired things would be a worse tool than none."""
    await _enrol_and_login(enrol, login)
    before = (
        await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT),
        await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION),
        unit_of_work.commits,
    )

    await status.execute(ACCOUNT, BROKER)

    assert (
        await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT),
        await unit_of_work.credentials.get(ACCOUNT, BROKER, CredentialPurpose.SESSION),
        unit_of_work.commits,
    ) == before


async def test_a_status_report_carries_no_secret(
    enrol: EnrolBrokerApplication,
    login: EstablishBrokerSession,
    status: DescribeBrokerAuthentication,
) -> None:
    """It is the command most likely to be pasted into a bug report."""
    await _enrol_and_login(enrol, login)

    rendered = repr(await status.execute(ACCOUNT, BROKER))

    assert SESSION_MATERIAL not in rendered
    assert APPLICATION_MATERIAL not in rendered
    assert IDENTIFIER not in rendered
