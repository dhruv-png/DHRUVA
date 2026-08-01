"""Envelope encryption behaviour (ADR-070, ADR-020, ADR-038).

The tests that matter here are the negative ones. A cipher that round-trips is
easy; what ADR-070 actually buys is that tampering fails loudly, that the master
key never reaches a row, and that no plaintext is obtainable without a
KeyProvider. Those are the properties asserted below.

The associated-data section closes TD-S06-6. Until the credential table existed
there was nothing stable to bind a ciphertext to, so a ciphertext lifted from one
row into another decrypted perfectly well. It no longer does, and the tests that
prove it are the ones that would have passed before.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import os
from typing import Any, Final, cast

import pytest

from dhruva.contexts.platform.domain.identity.keys import KeyProvider
from dhruva.contexts.platform.infrastructure.crypto import (
    DATA_KEY_BYTES,
    MASTER_KEY_BYTES,
    NONCE_BYTES,
    EncryptedSecret,
    MasterKeyProvider,
    decrypt_secret,
    encrypt_secret,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import ConfigurationError, SafetyError

PLAINTEXT: Final = b"kite-api-secret-abc123"

#: Stands in for a real record binding. The shape a credential actually uses is
#: built by ``credential_associated_data`` and tested beside the aggregate; what
#: matters here is only that the value is authenticated.
AAD: Final = b"record:one"
OTHER_AAD: Final = b"record:two"


def _master_key() -> SecretValue:
    """Return freshly generated, correctly encoded master key material."""
    return SecretValue(base64.b64encode(os.urandom(MASTER_KEY_BYTES)).decode(), register=False)


@pytest.fixture
def provider() -> MasterKeyProvider:
    """Return a provider over a random master key, new for every test."""
    return MasterKeyProvider(_master_key())


# --------------------------------------------------------------------------- #
# The port
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_adapter_satisfies_the_key_provider_port(provider: MasterKeyProvider) -> None:
    """Structural conformance is the whole substitution argument of ADR-070."""
    assert isinstance(provider, KeyProvider)


@pytest.mark.unit
def test_a_wrapped_key_round_trips(provider: MasterKeyProvider) -> None:
    """Wrapping and unwrapping are inverses under the same master key."""
    data_key = os.urandom(DATA_KEY_BYTES)

    assert provider.unwrap_key(provider.wrap_key(data_key)) == data_key


@pytest.mark.unit
def test_wrapping_never_returns_the_input(provider: MasterKeyProvider) -> None:
    """The port's contract says so explicitly, and it is worth one assertion."""
    data_key = os.urandom(DATA_KEY_BYTES)
    wrapped = provider.wrap_key(data_key)

    assert wrapped != data_key
    assert data_key not in wrapped


@pytest.mark.unit
def test_wrapping_the_same_key_twice_produces_different_ciphertext(
    provider: MasterKeyProvider,
) -> None:
    """A fresh nonce per operation, which GCM requires and reuse would destroy."""
    data_key = os.urandom(DATA_KEY_BYTES)

    assert provider.wrap_key(data_key) != provider.wrap_key(data_key)


@pytest.mark.unit
def test_the_key_provider_port_takes_no_associated_data() -> None:
    """ADR-070's substitution argument, asserted as a signature.

    Binding the data-key wrap as well as the ciphertext would mean widening this
    port, and a KMS adapter expresses encryption context as a string map rather
    than as bytes. The narrow port is the deliberate choice, so it is worth a
    test that notices someone widening it.
    """
    for method in (KeyProvider.wrap_key, KeyProvider.unwrap_key):
        parameters = set(inspect.signature(method).parameters)
        assert "associated_data" not in parameters


# --------------------------------------------------------------------------- #
# Envelope round trip
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_secret_round_trips_through_the_envelope(provider: MasterKeyProvider) -> None:
    """The whole two-level construction, end to end."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    assert decrypt_secret(sealed, provider, associated_data=AAD) == PLAINTEXT


@pytest.mark.unit
@pytest.mark.parametrize(
    "plaintext",
    [b"", b"\x00", b"\xff" * 4096, "पासवर्ड".encode()],
    ids=["empty", "null-byte", "long", "non-ascii"],
)
def test_the_envelope_is_byte_exact(provider: MasterKeyProvider, plaintext: bytes) -> None:
    """Every byte matters in a credential, including none of them."""
    sealed = encrypt_secret(plaintext, provider, associated_data=AAD)

    assert decrypt_secret(sealed, provider, associated_data=AAD) == plaintext


@pytest.mark.unit
def test_each_credential_gets_its_own_data_key(provider: MasterKeyProvider) -> None:
    """Reuse would make one compromised key open more than one record."""
    first = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    second = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    assert first.wrapped_data_key != second.wrapped_data_key
    assert first.ciphertext != second.ciphertext
    assert provider.unwrap_key(first.wrapped_data_key) != provider.unwrap_key(
        second.wrapped_data_key
    )


@pytest.mark.unit
def test_the_stored_row_holds_neither_the_master_key_nor_a_plaintext_data_key() -> None:
    """ADR-070's central claim about what a row contains."""
    raw = os.urandom(MASTER_KEY_BYTES)
    provider = MasterKeyProvider(SecretValue(base64.b64encode(raw).decode(), register=False))

    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    data_key = provider.unwrap_key(sealed.wrapped_data_key)

    stored = sealed.ciphertext + sealed.wrapped_data_key
    assert raw not in stored
    assert data_key not in stored
    assert PLAINTEXT not in stored


@pytest.mark.unit
def test_the_encrypted_secret_is_frozen(provider: MasterKeyProvider) -> None:
    """The two fields are only correct together."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    with pytest.raises(dataclasses.FrozenInstanceError):
        sealed.ciphertext = b"replaced"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Associated data -- TD-S06-6
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_ciphertext_will_not_open_under_a_different_binding(
    provider: MasterKeyProvider,
) -> None:
    """The debt, closed. This is the assertion TD-S06-6 was waiting for.

    Same master key, same wrapped data key, entirely intact ciphertext -- and it
    still refuses, because the binding it was sealed under is not the binding
    presented on read. That is what stops row A's sealed values being lifted into
    row B.
    """
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    with pytest.raises(SafetyError):
        decrypt_secret(sealed, provider, associated_data=OTHER_AAD)


@pytest.mark.unit
def test_the_binding_is_not_stored_alongside_the_ciphertext(
    provider: MasterKeyProvider,
) -> None:
    """Associated data is authenticated, not encrypted, and not persisted.

    The reader recomputes it from the row. A copy travelling with the ciphertext
    would move with it, which would defeat the entire construction.
    """
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    assert AAD not in sealed.ciphertext + sealed.wrapped_data_key


@pytest.mark.unit
def test_the_same_plaintext_under_two_bindings_gives_two_ciphertexts(
    provider: MasterKeyProvider,
) -> None:
    """A fresh data key and nonce per call, unchanged by the binding."""
    first = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    second = encrypt_secret(PLAINTEXT, provider, associated_data=OTHER_AAD)

    assert first.ciphertext != second.ciphertext


@pytest.mark.unit
def test_an_empty_binding_is_a_binding_and_not_an_absent_one(
    provider: MasterKeyProvider,
) -> None:
    """``b""`` authenticates as itself, so it cannot be opened as anything else.

    Worth stating, because ``b""`` is what a default would have been. It is not a
    neutral value that opens under any binding -- but nor does it bind to a
    record, which is why the parameter is required rather than defaulted.
    """
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=b"")

    assert decrypt_secret(sealed, provider, associated_data=b"") == PLAINTEXT
    with pytest.raises(SafetyError):
        decrypt_secret(sealed, provider, associated_data=AAD)


@pytest.mark.unit
def test_the_binding_cannot_be_passed_positionally(provider: MasterKeyProvider) -> None:
    """Keyword-only, so a provider and a binding cannot be transposed.

    ``encrypt_secret(plaintext, provider, aad)`` and
    ``encrypt_secret(plaintext, aad, provider)`` are both plausible at a glance;
    only one is right, and a positional parameter would let the other run.

    Called through an untyped alias deliberately. The type checker already
    refuses this at every real call site, and the point of the test is what
    happens to the caller who is not type-checked.
    """
    misuse = cast("Any", encrypt_secret)

    with pytest.raises(TypeError):
        misuse(PLAINTEXT, provider, AAD)


@pytest.mark.unit
def test_the_binding_cannot_be_omitted(provider: MasterKeyProvider) -> None:
    """Required, not defaulted: an unbound ciphertext is the defect, not a mode."""
    misuse = cast("Any", encrypt_secret)

    with pytest.raises(TypeError):
        misuse(PLAINTEXT, provider)


# --------------------------------------------------------------------------- #
# Tampering -- the reason ADR-070 requires authenticated encryption
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_tampered_ciphertext_is_refused(provider: MasterKeyProvider) -> None:
    """One flipped bit must fail authentication, not decrypt to garbage."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    flipped = bytearray(sealed.ciphertext)
    flipped[-1] ^= 0x01

    with pytest.raises(SafetyError):
        decrypt_secret(
            EncryptedSecret(bytes(flipped), sealed.wrapped_data_key),
            provider,
            associated_data=AAD,
        )


@pytest.mark.unit
def test_a_tampered_wrapped_key_is_refused(provider: MasterKeyProvider) -> None:
    """The outer level authenticates too, not only the inner one."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    flipped = bytearray(sealed.wrapped_data_key)
    flipped[-1] ^= 0x01

    with pytest.raises(SafetyError):
        decrypt_secret(
            EncryptedSecret(sealed.ciphertext, bytes(flipped)), provider, associated_data=AAD
        )


@pytest.mark.unit
def test_a_ciphertext_moved_between_records_is_refused(provider: MasterKeyProvider) -> None:
    """Mixing halves of two envelopes must not decrypt to anything."""
    first = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    second = encrypt_secret(b"a-different-secret", provider, associated_data=OTHER_AAD)

    with pytest.raises(SafetyError):
        decrypt_secret(
            EncryptedSecret(first.ciphertext, second.wrapped_data_key),
            provider,
            associated_data=AAD,
        )


@pytest.mark.unit
def test_another_master_key_cannot_open_the_envelope(provider: MasterKeyProvider) -> None:
    """Losing the master key destroys the vault, irreversibly and by design."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)

    with pytest.raises(SafetyError):
        decrypt_secret(sealed, MasterKeyProvider(_master_key()), associated_data=AAD)


@pytest.mark.unit
@pytest.mark.parametrize("length", [0, 1, NONCE_BYTES], ids=["empty", "one-byte", "nonce-only"])
def test_a_truncated_value_is_refused_rather_than_indexed(
    provider: MasterKeyProvider, length: int
) -> None:
    """Too short to hold a nonce is a refusal, not an IndexError."""
    with pytest.raises(SafetyError):
        provider.unwrap_key(b"\x00" * length)


@pytest.mark.unit
def test_refusal_reports_nothing_about_the_key(provider: MasterKeyProvider) -> None:
    """ADR-033: the error that reports a leak must not become one."""
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    flipped = bytearray(sealed.wrapped_data_key)
    flipped[0] ^= 0x01

    with pytest.raises(SafetyError) as caught:
        decrypt_secret(
            EncryptedSecret(sealed.ciphertext, bytes(flipped)), provider, associated_data=AAD
        )

    rendered = str(caught.value) + repr(caught.value.context)
    assert PLAINTEXT.decode() not in rendered
    assert "wrapped_data_key" in caught.value.context.values()


@pytest.mark.unit
def test_a_binding_mismatch_is_indistinguishable_from_a_tampered_ciphertext(
    provider: MasterKeyProvider,
) -> None:
    """The refusal must not confirm that the ciphertext itself was intact.

    A message naming the binding would tell whoever moved the row that they had
    the right bytes and the wrong home for them, which is more than a refusal
    should say.
    """
    sealed = encrypt_secret(PLAINTEXT, provider, associated_data=AAD)
    flipped = bytearray(sealed.ciphertext)
    flipped[-1] ^= 0x01

    with pytest.raises(SafetyError) as moved:
        decrypt_secret(sealed, provider, associated_data=OTHER_AAD)
    with pytest.raises(SafetyError) as tampered:
        decrypt_secret(
            EncryptedSecret(bytes(flipped), sealed.wrapped_data_key),
            provider,
            associated_data=AAD,
        )

    assert str(moved.value) == str(tampered.value)
    assert moved.value.context == tampered.value.context


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_placeholder_master_key_is_refused() -> None:
    """The shipped default must never encrypt anything."""
    with pytest.raises(ConfigurationError):
        MasterKeyProvider(SecretValue("change-me-local-only", register=False))


@pytest.mark.unit
@pytest.mark.parametrize("size", [16, 24, 31, 33, 64], ids=lambda n: f"{n}-bytes")
def test_a_master_key_of_the_wrong_length_is_refused(size: int) -> None:
    """AES-256 means 32 bytes; 16 and 24 are valid AES keys but not this one."""
    key = SecretValue(base64.b64encode(os.urandom(size)).decode(), register=False)

    with pytest.raises(ConfigurationError):
        MasterKeyProvider(key)


@pytest.mark.unit
def test_a_master_key_that_is_not_base64_is_refused() -> None:
    """Encoding is checked before length, and both are bootstrap failures."""
    with pytest.raises(ConfigurationError):
        MasterKeyProvider(SecretValue("not base64 at all!!", register=False))


@pytest.mark.unit
def test_the_configuration_failure_does_not_carry_the_key() -> None:
    """A bad key is still key material."""
    secret = "c2hvcnQ="  # noqa: S105 - base64 of b"short", deliberately too short

    with pytest.raises(ConfigurationError) as caught:
        MasterKeyProvider(SecretValue(secret, register=False))

    assert secret not in str(caught.value) + repr(caught.value.context)


@pytest.mark.unit
def test_a_misconfigured_process_fails_at_construction_not_at_first_use() -> None:
    """Bootstrap is a better place to discover this than the first credential read."""
    with pytest.raises(ConfigurationError):
        MasterKeyProvider(SecretValue("short", register=False))
