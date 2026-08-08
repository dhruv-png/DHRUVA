"""Sealing and opening a credential (ADR-070, ADR-033, ADR-037, TD-S06-6).

These two functions are the entire plaintext surface of the credential store, so
what is asserted here is mostly what they refuse to do: open a ciphertext that
belongs to another record, and hand back a secret the logging stack cannot
recognise.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import os
from datetime import UTC, datetime
from typing import Any, Final, cast

import pytest

from dhruva.contexts.platform.domain.identity.credentials import (
    Credential,
    CredentialPurpose,
    EncryptedSecret,
    credential_associated_data,
)
from dhruva.contexts.platform.infrastructure.crypto import (
    MASTER_KEY_BYTES,
    MasterKeyProvider,
    encrypt_secret,
    open_credential,
    seal_credential,
)
from dhruva.shared.config.secret import SecretValue, registered_secret_values
from dhruva.shared.errors import DataQualityError, SafetyError
from dhruva.shared.identity import AccountId, CredentialId

pytestmark = pytest.mark.unit

CREATED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
FIRST: Final = CredentialId.deterministic("credential", "first")
SECOND: Final = CredentialId.deterministic("credential", "second")
ACCOUNT: Final = AccountId.deterministic("primary")
BROKER: Final = "zerodha"
ENROLMENT: Final = CredentialPurpose.ENROLMENT
SESSION: Final = CredentialPurpose.SESSION


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return a provider over a random master key, new for every test."""
    key = SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)
    return MasterKeyProvider(key)


def _credential(
    secret: EncryptedSecret,
    credential_id: CredentialId = FIRST,
    purpose: CredentialPurpose = ENROLMENT,
) -> Credential:
    return Credential(
        credential_id=credential_id,
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=purpose,
        secret=secret,
        created_at=CREATED,
        updated_at=CREATED,
    )


def _sealed(
    provider: MasterKeyProvider,
    plaintext: str,
    credential_id: CredentialId,
    purpose: CredentialPurpose = ENROLMENT,
) -> Credential:
    sealed = seal_credential(
        SecretValue(plaintext, register=False),
        provider,
        credential_id=credential_id,
        account_id=ACCOUNT,
        broker=BROKER,
        purpose=purpose,
    )
    return _credential(sealed, credential_id, purpose)


def test_a_credential_round_trips(provider: MasterKeyProvider) -> None:
    """The ordinary path, and the only one that yields a plaintext."""
    credential = _sealed(provider, "kite-api-secret-abc123", FIRST)

    assert open_credential(credential, provider).reveal() == "kite-api-secret-abc123"


def test_an_opened_credential_is_registered_for_redaction(provider: MasterKeyProvider) -> None:
    """ADR-037: a secret the logging stack cannot recognise is one it cannot mask.

    Returning a bare ``str`` would work identically right up until somebody
    interpolated it into a log line, which is the failure the redaction registry
    exists to survive.
    """
    plaintext = f"kite-secret-{os.urandom(8).hex()}"

    opened = open_credential(_sealed(provider, plaintext, FIRST), provider)

    assert isinstance(opened, SecretValue)
    assert opened.reveal() in registered_secret_values()


def test_a_ciphertext_lifted_into_another_record_will_not_open(
    provider: MasterKeyProvider,
) -> None:
    """TD-S06-6, at the layer a caller actually uses.

    The sealed values are untouched and the master key is the same one. Only the
    row they are presented as has changed, and that is enough.
    """
    victim = _sealed(provider, "kite-api-secret-abc123", FIRST)
    impostor = _credential(victim.secret, credential_id=SECOND)

    with pytest.raises(SafetyError):
        open_credential(impostor, provider)


def test_a_credential_re_parented_to_another_account_will_not_open(
    provider: MasterKeyProvider,
) -> None:
    """ADR-004's tenant column is bound too, not merely stored."""
    victim = _sealed(provider, "kite-api-secret-abc123", FIRST)
    moved = dataclasses.replace(victim, account_id=AccountId.deterministic("secondary"))

    with pytest.raises(SafetyError):
        open_credential(moved, provider)


def test_a_credential_relabelled_to_another_broker_will_not_open(
    provider: MasterKeyProvider,
) -> None:
    """The third bound fact, asserted for the same reason as the other two."""
    victim = _sealed(provider, "kite-api-secret-abc123", FIRST)
    relabelled = dataclasses.replace(victim, broker="another")

    with pytest.raises(SafetyError):
        open_credential(relabelled, provider)


def test_a_session_ciphertext_presented_as_enrolment_material_will_not_open(
    provider: MasterKeyProvider,
) -> None:
    """ADR-077's central claim, and the one worth testing hardest.

    Same account, same broker, same key, same credential identity. Only the
    purpose differs -- and without it in the binding this would decrypt cleanly,
    so the system would read a token that expires tomorrow as the owner's
    long-lived API secret.
    """
    session = _sealed(provider, "session-token-abc123", FIRST, SESSION)
    presented_as_enrolment = dataclasses.replace(session, purpose=ENROLMENT)

    with pytest.raises(SafetyError):
        open_credential(presented_as_enrolment, provider)


def test_an_enrolment_ciphertext_presented_as_a_session_will_not_open(
    provider: MasterKeyProvider,
) -> None:
    """The same refusal in the other direction.

    Asserted separately because the two failures have different consequences: a
    session read as enrolment material is a secret that silently expires, and
    enrolment material read as a session is a long-lived secret handed to a code
    path that treats it as disposable.
    """
    enrolment = _sealed(provider, "kite-api-secret-abc123", FIRST, ENROLMENT)
    presented_as_session = dataclasses.replace(enrolment, purpose=SESSION)

    with pytest.raises(SafetyError):
        open_credential(presented_as_session, provider)


def test_two_purposes_seal_the_same_plaintext_to_different_ciphertexts(
    provider: MasterKeyProvider,
) -> None:
    """Each opens only as itself, and neither is a copy of the other."""
    plaintext = "identical-secret-material"
    enrolment = _sealed(provider, plaintext, FIRST, ENROLMENT)
    session = _sealed(provider, plaintext, SECOND, SESSION)

    assert enrolment.secret.ciphertext != session.secret.ciphertext
    assert open_credential(enrolment, provider).reveal() == plaintext
    assert open_credential(session, provider).reveal() == plaintext


def test_sealing_offers_no_way_to_omit_a_purpose() -> None:
    """A default would let a caller seal material it never classified.

    Whatever the default was, some caller would take it by accident, and the row
    it wrote would be bound to a lifecycle nobody chose.
    """
    purpose = inspect.signature(seal_credential).parameters["purpose"]

    assert purpose.default is inspect.Parameter.empty
    assert purpose.kind is inspect.Parameter.KEYWORD_ONLY


def test_another_master_key_cannot_open_a_credential(provider: MasterKeyProvider) -> None:
    """The binding is an addition to the key hierarchy, not a replacement for it."""
    credential = _sealed(provider, "kite-api-secret-abc123", FIRST)
    stranger = MasterKeyProvider(
        SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)
    )

    with pytest.raises(SafetyError):
        open_credential(credential, stranger)


def test_opening_offers_no_way_to_supply_a_binding() -> None:
    """The misuse is designed out rather than documented against.

    ``open_credential`` derives the associated data from the credential it is
    given, so there is no argument through which a caller could present row A's
    ciphertext under row B's binding.
    """
    parameters = set(inspect.signature(open_credential).parameters)

    assert parameters == {"credential", "key_provider"}


def test_opening_requires_a_key_provider() -> None:
    """ADR-070: no code path yields a plaintext without one.

    Called through an untyped alias: the type checker already refuses this, and
    what is under test is that the runtime does too.
    """
    misuse = cast("Any", open_credential)

    with pytest.raises(TypeError):
        misuse(_credential(EncryptedSecret(b"x" * 32, b"y" * 32)))


def test_a_credential_that_is_not_utf8_is_a_data_quality_fault(
    provider: MasterKeyProvider,
) -> None:
    """Reachable only by bypassing ``seal_credential``, and reported rather than hidden.

    ``errors="replace"`` would hand back a corrupted secret that authenticates
    perfectly, which is worse than a refusal: the caller would send it to a
    broker and read the rejection as a credential problem.
    """
    sealed = encrypt_secret(
        b"\xff\xfe not utf-8",
        provider,
        associated_data=credential_associated_data(FIRST, ACCOUNT, BROKER, ENROLMENT),
    )

    with pytest.raises(DataQualityError) as caught:
        open_credential(_credential(sealed, FIRST), provider)

    assert "\\xff" not in str(caught.value)
    assert caught.value.context["broker"] == BROKER
