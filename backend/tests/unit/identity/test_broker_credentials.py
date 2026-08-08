"""Storing and reading broker credentials, one purpose at a time (ADR-077).

The use cases here are deliberately generic -- nothing in them knows what a Kite
API secret is -- so what is under test is the lifecycle separation itself: that
writing one purpose cannot disturb the other, that a rotation stays inside the
lifecycle it started in, and that no plaintext escapes through anything the
caller is handed back.

Sealing is done with the real cipher rather than a stub. A fake sealer would let
these tests pass while the binding was wrong, which is the one failure mode that
matters: the separation is only real if a cipher can tell the two purposes apart.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from dhruva.contexts.platform.application.identity.broker_credentials import (
    GetBrokerCredential,
    StoreBrokerCredential,
    StoreBrokerCredentialCommand,
    StoredCredential,
)
from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
from dhruva.contexts.platform.infrastructure.crypto import (
    MASTER_KEY_BYTES,
    MasterKeyProvider,
    open_credential,
    seal_credential,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import NotFoundError
from dhruva.shared.identity import AccountId, CredentialId
from tests.unit.identity.fakes import FakeUnitOfWork

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT: Final = AccountId.deterministic("primary")
BROKER: Final = "zerodha"
ENROLMENT: Final = CredentialPurpose.ENROLMENT
SESSION: Final = CredentialPurpose.SESSION
AT: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)

#: Obviously synthetic, and never a real secret. The shape is irrelevant to the
#: cipher, and a fixture that looked like real broker material would be one more
#: place a reader has to check before believing nothing was committed.
ENROLMENT_MATERIAL: Final = "synthetic-enrolment-secret"
SESSION_MATERIAL: Final = "synthetic-session-secret"


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return a provider over a random master key, new for every test."""
    key = SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)
    return MasterKeyProvider(key)


@pytest.fixture
def unit_of_work() -> FakeUnitOfWork:
    """Return the single transaction every use case in a test shares."""
    return FakeUnitOfWork()


@pytest.fixture
def factory(unit_of_work: FakeUnitOfWork) -> Callable[[AccountId], FakeUnitOfWork]:
    """Return a factory handing out that one transaction, so state persists."""
    return lambda _account_id: unit_of_work


@pytest.fixture
def store(
    factory: Callable[[AccountId], FakeUnitOfWork], provider: MasterKeyProvider
) -> StoreBrokerCredential:
    """Return the write use case, sealing through the injected port.

    The real ``seal_credential`` is passed where a ``CredentialSealer`` is
    expected and the type checker accepts it with no cast. That is worth
    noticing: it is the evidence that the port describes the function rather
    than the function having been reshaped to fit an invented port.
    """
    return StoreBrokerCredential(factory, provider, seal_credential)


@pytest.fixture
def read(factory: Callable[[AccountId], FakeUnitOfWork]) -> GetBrokerCredential:
    """Return the read use case over the same transaction."""
    return GetBrokerCredential(factory)


def _command(
    purpose: CredentialPurpose,
    secret: str,
    *,
    at: datetime = AT,
    credential_id: CredentialId | None = None,
) -> StoreBrokerCredentialCommand:
    return StoreBrokerCredentialCommand(
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=purpose,
        secret=SecretValue(secret, register=False),
        at=at,
        credential_id=credential_id or CredentialId.new(),
    )


# --------------------------------------------------------------------------- #
# Storing
# --------------------------------------------------------------------------- #


async def test_a_stored_credential_opens_to_what_was_handed_in(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """The ordinary path, proved by decryption rather than by equality of bytes."""
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    sealed = await read.sealed(ACCOUNT, BROKER, ENROLMENT)

    assert open_credential(sealed, provider).reveal() == ENROLMENT_MATERIAL


async def test_storing_commits(store: StoreBrokerCredential, unit_of_work: FakeUnitOfWork) -> None:
    """A credential the caller was told was stored must actually be durable."""
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    assert unit_of_work.commits == 1


async def test_the_two_purposes_are_stored_independently(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """One broker, one account, two secrets that never see each other."""
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))
    await store.execute(_command(SESSION, SESSION_MATERIAL))

    enrolment = await read.sealed(ACCOUNT, BROKER, ENROLMENT)
    session = await read.sealed(ACCOUNT, BROKER, SESSION)

    assert open_credential(enrolment, provider).reveal() == ENROLMENT_MATERIAL
    assert open_credential(session, provider).reveal() == SESSION_MATERIAL
    assert enrolment.credential_id != session.credential_id


async def test_replacing_one_purpose_leaves_the_other_untouched(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """The operational point of ADR-077, asserted at the use-case layer.

    A session is replaced every morning. If that write reached the enrolment
    credential -- through a lookup that ignored the purpose, or a repository that
    keyed on account and broker alone -- the owner's long-lived secret would be
    overwritten by a routine login.
    """
    enrolment = await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))
    await store.execute(_command(SESSION, SESSION_MATERIAL))

    await store.execute(
        _command(SESSION, "synthetic-session-secret-day-two", at=AT + timedelta(days=1))
    )

    unchanged = await read.summary(ACCOUNT, BROKER, ENROLMENT)
    assert unchanged == enrolment
    assert unchanged is not None
    assert unchanged.version == 1
    assert unchanged.rotated_at is None
    assert (
        open_credential(await read.sealed(ACCOUNT, BROKER, ENROLMENT), provider).reveal()
        == ENROLMENT_MATERIAL
    )


async def test_replacing_the_enrolment_leaves_the_session_untouched(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """The same independence in the other direction.

    Asserted separately because the two directions fail differently. A session
    write reaching enrolment destroys the owner's long-lived secret; an enrolment
    rotation reaching the session silently invalidates a live login, and the
    operator would see an authentication failure with no obvious cause.
    """
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))
    session = await store.execute(_command(SESSION, SESSION_MATERIAL))

    await store.execute(
        _command(ENROLMENT, "synthetic-enrolment-secret-rotated", at=AT + timedelta(days=30))
    )

    unchanged = await read.summary(ACCOUNT, BROKER, SESSION)
    assert unchanged == session
    assert unchanged is not None
    assert unchanged.version == 1
    assert unchanged.rotated_at is None
    assert (
        open_credential(await read.sealed(ACCOUNT, BROKER, SESSION), provider).reveal()
        == SESSION_MATERIAL
    )


async def test_storing_again_rotates_within_the_same_purpose(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """Enrolment and rotation are the same call, and the second is a new version."""
    first = await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    second = await store.execute(
        _command(ENROLMENT, "synthetic-enrolment-secret-rotated", at=AT + timedelta(days=30))
    )

    assert second.version == first.version + 1
    assert second.rotated_at == AT + timedelta(days=30)
    assert (
        open_credential(await read.sealed(ACCOUNT, BROKER, ENROLMENT), provider).reveal()
        == "synthetic-enrolment-secret-rotated"
    )


async def test_a_rotation_keeps_the_original_credential_identity(
    store: StoreBrokerCredential, read: GetBrokerCredential, provider: MasterKeyProvider
) -> None:
    """Because the ciphertext is bound to it.

    The command carries a fresh identity for the new-credential case, and a
    rotation that adopted it would seal under a binding the stored row cannot
    present -- writing ciphertext that could never be opened again. Asserting the
    identity is not enough on its own; the credential has to still open.
    """
    first = await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    second = await store.execute(
        _command(
            ENROLMENT,
            "synthetic-enrolment-secret-rotated",
            at=AT + timedelta(days=30),
            credential_id=CredentialId.new(),
        )
    )

    assert second.credential_id == first.credential_id
    assert (
        open_credential(await read.sealed(ACCOUNT, BROKER, ENROLMENT), provider).reveal()
        == "synthetic-enrolment-secret-rotated"
    )


async def test_a_rotation_cannot_move_a_credential_to_the_other_purpose(
    store: StoreBrokerCredential, read: GetBrokerCredential
) -> None:
    """A write for one purpose creates or replaces only that purpose's row.

    Storing a session when only an enrolment exists is a first write, not a
    re-labelling: the enrolment credential is still there afterwards, and the
    session is a separate row with its own identity.
    """
    enrolment = await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    session = await store.execute(_command(SESSION, SESSION_MATERIAL))

    assert session.credential_id != enrolment.credential_id
    assert session.version == 1
    assert await read.summary(ACCOUNT, BROKER, ENROLMENT) == enrolment


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def test_a_summary_carries_no_sealed_or_plaintext_material(
    store: StoreBrokerCredential, read: GetBrokerCredential
) -> None:
    """ADR-070, asserted on the shape of what an ordinary caller receives.

    Most callers want to know whether a credential exists and when it last
    changed. Handing them ciphertext as well would put sealed material into log
    lines and error contexts for no reason, so the summary type does not carry
    any.
    """
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    summary = await read.summary(ACCOUNT, BROKER, ENROLMENT)

    assert summary is not None
    fields = set(StoredCredential.__dataclass_fields__)
    assert fields.isdisjoint({"secret", "ciphertext", "wrapped_data_key", "plaintext"})
    assert ENROLMENT_MATERIAL not in repr(summary)


async def test_a_missing_credential_summarises_as_none(read: GetBrokerCredential) -> None:
    """Nothing enrolled and no session established are both ordinary states.

    A caller distinguishes them by which purpose it asked about, which is only
    possible because the lookup takes one.
    """
    assert await read.summary(ACCOUNT, BROKER, ENROLMENT) is None
    assert await read.summary(ACCOUNT, BROKER, SESSION) is None


async def test_a_lookup_never_returns_the_other_purpose(
    store: StoreBrokerCredential, read: GetBrokerCredential
) -> None:
    """The failure this rules out is silent: an API secret used as a token."""
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    assert await read.summary(ACCOUNT, BROKER, SESSION) is None


async def test_asking_for_openable_material_that_is_absent_raises(
    read: GetBrokerCredential,
) -> None:
    """A caller reaching for a secret has already decided it needs one.

    Returning ``None`` here would push the decision one call further out, where
    it would eventually be missed; the error names the broker and the purpose so
    the operator knows which of the two is missing.
    """
    with pytest.raises(NotFoundError) as caught:
        await read.sealed(ACCOUNT, BROKER, SESSION)

    assert caught.value.context["purpose"] == "SESSION"
    assert caught.value.context["broker"] == BROKER


async def test_reading_never_returns_a_plaintext(
    store: StoreBrokerCredential, read: GetBrokerCredential
) -> None:
    """Neither read yields a secret; opening one needs a key provider elsewhere.

    Asserted over the returned objects rather than a chosen field, because the
    failure worth catching is a plaintext appearing somewhere nobody thought to
    look -- a debug attribute, a cached property, a ``repr``.
    """
    await store.execute(_command(ENROLMENT, ENROLMENT_MATERIAL))

    summary = await read.summary(ACCOUNT, BROKER, ENROLMENT)
    sealed = await read.sealed(ACCOUNT, BROKER, ENROLMENT)

    assert ENROLMENT_MATERIAL not in repr(summary)
    assert ENROLMENT_MATERIAL not in repr(sealed)
    assert ENROLMENT_MATERIAL.encode() not in sealed.secret.ciphertext
    assert ENROLMENT_MATERIAL.encode() not in sealed.secret.wrapped_data_key


async def test_the_use_cases_hold_no_key_material_for_reading(
    read: GetBrokerCredential,
) -> None:
    """The read side takes no ``KeyProvider``, so it could not decrypt if asked.

    This is what makes "the only plaintext path is ``open_credential``"
    structural rather than a convention a future change could quietly break.
    """
    assert "key_provider" not in GetBrokerCredential.__init__.__annotations__
    assert set(read.__slots__) == {"_unit_of_work_factory"}
