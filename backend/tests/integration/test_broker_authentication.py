"""Broker authentication against real PostgreSQL (ADR-058, ADR-070, ADR-077).

The unit suite proves the use cases behave; it cannot prove the two credentials
actually coexist as rows, that ``BYTEA`` returns the sealed document byte for
byte, or that the purpose-aware unique constraint permits exactly the pair this
flow writes and no more.

No live Zerodha call. The broker is a double here as it is in the unit suite --
what is real is the database, the cipher and the constraint.
"""

from __future__ import annotations

import base64
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
from dhruva.contexts.platform.infrastructure.persistence.credentials import CredentialRepository
from dhruva.contexts.platform.infrastructure.persistence.factories import CredentialFactory
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import ConflictError, NotFoundError
from dhruva.shared.identity import AccountId
from dhruva.shared.time.clock import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

FACTORY: Final = CredentialFactory()
BROKER: Final = "zerodha"
ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER_ACCOUNT: Final = AccountId.deterministic("someone-else")

#: Obviously synthetic. Nothing here is or resembles real broker material.
IDENTIFIER: Final = "synthetic-api-identifier"
APPLICATION_MATERIAL: Final = "synthetic-application-material"
REQUEST_MATERIAL: Final = "synthetic-request-material"
SESSION_MATERIAL: Final = "synthetic-session-material"

ISSUED: Final = datetime(2026, 8, 8, 4, 30, tzinfo=UTC)
EXPIRES: Final = datetime(2026, 8, 9, 0, 30, tzinfo=UTC)

#: Fixed rather than random so a failure reproduces, and still real key material.
MASTER_KEY: Final = SecretValue(
    base64.b64encode(bytes(range(MASTER_KEY_BYTES))).decode(), register=False
)


class FakeBroker:
    """Answers the handshake without a network call."""

    def __init__(self) -> None:
        self.issues = SESSION_MATERIAL

    def login_url(self, application: BrokerApplication) -> str:
        """Return a URL carrying only the public half."""
        return f"https://broker.invalid/login?api_key={application.identifier.reveal()}"

    async def exchange(
        self,
        application: BrokerApplication,  # noqa: ARG002 - part of the port's shape
        request_token: SecretValue,  # noqa: ARG002 - same
    ) -> BrokerSessionMaterial:
        """Return session material for the stored session."""
        return BrokerSessionMaterial(
            token=SecretValue(self.issues, register=False),
            broker_user_id="XX0000",
            issued_at=ISSUED,
            expires_at=EXPIRES,
        )


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return the key provider these tests seal and open with."""
    return MasterKeyProvider(MASTER_KEY)


@pytest.fixture
def clock() -> FrozenClock:
    """Return a clock inside the session's lifetime."""
    return FrozenClock(ISSUED + timedelta(hours=1))


def _application(secret: str = APPLICATION_MATERIAL) -> BrokerApplication:
    return BrokerApplication(
        identifier=SecretValue(IDENTIFIER, register=False),
        secret=SecretValue(secret, register=False),
    )


def _factory(session: AsyncSession) -> Callable[[AccountId], object]:
    """Return a unit-of-work factory over the test's own transaction.

    Every use case in a test shares the one session the fixture owns, so the
    fixture's rollback undoes the whole flow. That is the same arrangement the
    other integration suites use, and it is why these tests commit nothing.
    """

    def build(_account: AccountId) -> object:
        return _SessionUnitOfWork(session)

    return build


class _SessionUnitOfWork:
    """A unit of work over an already-open session, committing by flushing.

    The real one owns its session; here the fixture does, so ``commit`` flushes
    instead. Writing this rather than reaching for the real class is deliberate:
    a real commit would escape the fixture's transaction and leak rows into the
    next test, which is the failure mode the conftest's cleanup notes warn about.
    """

    __slots__ = ("_session", "credentials")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.credentials = CredentialRepository(session, FACTORY)

    async def __aenter__(self) -> _SessionUnitOfWork:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        """Flush rather than commit, so the fixture keeps ownership."""
        await self._session.flush()

    async def rollback(self) -> None:
        """No-op: the fixture rolls the whole transaction back."""
        return


async def _enrol_and_login(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    factory = _factory(session)
    await EnrolBrokerApplication(factory, provider, seal_credential).execute(  # type: ignore[arg-type]
        ACCOUNT, BROKER, _application(), at=ISSUED
    )
    await EstablishBrokerSession(
        factory,  # type: ignore[arg-type]
        provider,
        seal_credential,
        open_credential,
        FakeBroker(),
        clock,
    ).execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))


# --------------------------------------------------------------------------- #
# The flow, as rows
# --------------------------------------------------------------------------- #


async def test_enrolment_and_session_are_two_rows_for_one_broker(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """ADR-077's purpose-aware uniqueness, exercised by the flow that needs it.

    Before the migration this was an integrity error, which is why the login had
    nowhere to store a token except on top of the owner's API secret.
    """
    await _enrol_and_login(session, provider, clock)
    session.expunge_all()
    repository = CredentialRepository(session, FACTORY)

    enrolment = await repository.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    stored_session = await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert enrolment is not None
    assert stored_session is not None
    assert enrolment.credential_id != stored_session.credential_id


async def test_the_sealed_documents_survive_bytea_intact(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """Every field, through the driver and back, still openable.

    Equality of the byte columns would pass even if the driver returned a
    ``memoryview`` that compared equal and decrypted differently; opening both
    documents is what proves the rows are intact all the way down.
    """
    await _enrol_and_login(session, provider, clock)
    session.expunge_all()
    repository = CredentialRepository(session, FACTORY)

    enrolment = await repository.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    stored_session = await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert enrolment is not None
    assert stored_session is not None

    application = parse_broker_application(open_credential(enrolment, provider))
    live = parse_broker_session(open_credential(stored_session, provider))
    assert application.identifier.reveal() == IDENTIFIER
    assert application.secret.reveal() == APPLICATION_MATERIAL
    assert live.token.reveal() == SESSION_MATERIAL
    assert live.broker_user_id == "XX0000"
    assert live.issued_at == ISSUED
    assert live.expires_at == EXPIRES


async def test_no_plaintext_reaches_the_table(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """Asserted over the stored bytes, which is where it would actually show."""
    await _enrol_and_login(session, provider, clock)
    session.expunge_all()
    repository = CredentialRepository(session, FACTORY)

    for purpose in (CredentialPurpose.ENROLMENT, CredentialPurpose.SESSION):
        stored = await repository.get(ACCOUNT, BROKER, purpose)
        assert stored is not None
        blob = stored.secret.ciphertext + stored.secret.wrapped_data_key
        for material in (APPLICATION_MATERIAL, SESSION_MATERIAL, IDENTIFIER, REQUEST_MATERIAL):
            assert material.encode() not in blob


# --------------------------------------------------------------------------- #
# Independent rotation
# --------------------------------------------------------------------------- #


async def test_a_second_login_replaces_only_the_session_row(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """The daily act, against a real unique constraint and a real version column."""
    await _enrol_and_login(session, provider, clock)
    await session.flush()
    repository = CredentialRepository(session, FACTORY)
    enrolment_before = await repository.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)

    broker = FakeBroker()
    broker.issues = "synthetic-session-material-day-two"
    await EstablishBrokerSession(
        _factory(session),  # type: ignore[arg-type]
        provider,
        seal_credential,
        open_credential,
        broker,
        clock,
    ).execute(ACCOUNT, BROKER, SecretValue("synthetic-request-material-two", register=False))
    session.expunge_all()

    enrolment_after = await repository.get(ACCOUNT, BROKER, CredentialPurpose.ENROLMENT)
    stored_session = await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert enrolment_after == enrolment_before
    assert stored_session is not None
    assert stored_session.version == 2
    assert (
        parse_broker_session(open_credential(stored_session, provider)).token.reveal()
        == "synthetic-session-material-day-two"
    )


async def test_rotating_the_application_leaves_the_session_row_alone(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """The other direction: a routine secret rotation must not end a live login."""
    await _enrol_and_login(session, provider, clock)
    await session.flush()
    repository = CredentialRepository(session, FACTORY)
    session_before = await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)

    await EnrolBrokerApplication(_factory(session), provider, seal_credential).execute(  # type: ignore[arg-type]
        ACCOUNT,
        BROKER,
        _application("synthetic-application-material-rotated"),
        at=ISSUED + timedelta(days=30),
    )
    session.expunge_all()

    assert await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION) == session_before


async def test_a_lost_update_on_the_session_is_refused(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """ADR-057 still applies to a credential written by this flow.

    Two logins from the same loaded state: the second must be refused rather
    than overwrite the first, or a race between two terminals would leave a row
    whose token nobody recorded.
    """
    await _enrol_and_login(session, provider, clock)
    await session.flush()
    repository = CredentialRepository(session, FACTORY)
    stale = await repository.get(ACCOUNT, BROKER, CredentialPurpose.SESSION)
    assert stale is not None

    replacement = seal_credential(
        SecretValue("synthetic-session-material-replaced", register=False),
        provider,
        credential_id=stale.credential_id,
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=CredentialPurpose.SESSION,
    )
    await repository.update(stale.resealed(replacement, at=ISSUED + timedelta(hours=2)))
    await session.flush()

    with pytest.raises(ConflictError):
        await repository.update(stale.resealed(replacement, at=ISSUED + timedelta(hours=3)))


# --------------------------------------------------------------------------- #
# Absence and isolation
# --------------------------------------------------------------------------- #


async def test_status_reports_absence_before_anything_is_enrolled(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """Reading an empty table is an ordinary answer, not an error."""
    status = DescribeBrokerAuthentication(
        _factory(session),  # type: ignore[arg-type]
        provider,
        open_credential,
        clock,
    )

    assert (await status.execute(ACCOUNT, BROKER)).state is BrokerAuthState.ENROLMENT_MISSING


async def test_logging_in_without_enrolment_raises_against_the_database(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """The missing row is the signal, and the error names what to do about it."""
    login = EstablishBrokerSession(
        _factory(session),  # type: ignore[arg-type]
        provider,
        seal_credential,
        open_credential,
        FakeBroker(),
        clock,
    )

    with pytest.raises(NotFoundError):
        await login.execute(ACCOUNT, BROKER, SecretValue(REQUEST_MATERIAL, register=False))


async def test_another_account_reads_none_of_this_one(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """ADR-004, against the real tenant column rather than a fake's dictionary."""
    await _enrol_and_login(session, provider, clock)
    session.expunge_all()
    status = DescribeBrokerAuthentication(
        _factory(session),  # type: ignore[arg-type]
        provider,
        open_credential,
        clock,
    )

    assert (await status.execute(OTHER_ACCOUNT, BROKER)).state is BrokerAuthState.ENROLMENT_MISSING
    assert (await status.execute(ACCOUNT, BROKER)).state is BrokerAuthState.SUCCESS


async def test_a_reconnecting_reader_sees_the_stored_session(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """The point of storing it: a later process authenticates without a login.

    ``expunge_all`` is the closest this fixture gets to a new process -- every
    object is reloaded from the row rather than served from the identity map --
    and the assertion is that the reloaded session still opens and is still
    judged live.
    """
    await _enrol_and_login(session, provider, clock)
    await session.flush()
    session.expunge_all()

    status = DescribeBrokerAuthentication(
        _factory(session),  # type: ignore[arg-type]
        provider,
        open_credential,
        clock,
    )
    reported = await status.execute(ACCOUNT, BROKER)

    assert reported.state is BrokerAuthState.SUCCESS
    assert reported.broker_user_id == "XX0000"
    assert reported.expires_at == EXPIRES


async def test_an_expired_session_is_reported_as_expired_not_missing(
    session: AsyncSession, provider: MasterKeyProvider, clock: FrozenClock
) -> None:
    """The row is still there; what changed is the morning."""
    await _enrol_and_login(session, provider, clock)
    session.expunge_all()
    later = DescribeBrokerAuthentication(
        _factory(session),  # type: ignore[arg-type]
        provider,
        open_credential,
        FrozenClock(EXPIRES + timedelta(minutes=1)),
    )

    reported = await later.execute(ACCOUNT, BROKER)

    assert reported.state is BrokerAuthState.SESSION_EXPIRED
    assert reported.expires_at == EXPIRES


async def test_the_flow_writes_exactly_two_credential_rows(
    migrated: AsyncEngine,  # noqa: ARG001 - requests the migrated schema
    session: AsyncSession,
    provider: MasterKeyProvider,
    clock: FrozenClock,
) -> None:
    """Two, not three, and not one.

    Counted rather than inferred. A third row would mean a purpose leaked in
    somewhere; one would mean the session overwrote the enrolment, which is the
    bug ADR-077 exists to make impossible.
    """
    from sqlalchemy import text  # noqa: PLC0415 - test-only import

    await _enrol_and_login(session, provider, clock)
    await session.flush()

    count = await session.scalar(
        text("SELECT count(*) FROM credential WHERE account_id = :account"),
        {"account": ACCOUNT.value},
    )
    purposes = await session.scalars(
        text("SELECT purpose FROM credential WHERE account_id = :account ORDER BY purpose"),
        {"account": ACCOUNT.value},
    )
    assert count == 2
    assert list(purposes) == ["ENROLMENT", "SESSION"]
