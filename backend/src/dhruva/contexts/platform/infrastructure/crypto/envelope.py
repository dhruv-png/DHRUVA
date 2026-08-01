"""AES-256-GCM envelope encryption (ADR-070, implementing ADR-020).

The only module in the codebase that imports a cipher. Everything above it
reaches key material through the :class:`~dhruva.contexts.platform.domain.
identity.keys.KeyProvider` port, so the domain never learns whether a data key
was unwrapped from an environment variable or by a KMS -- which is the entire
argument ADR-070 makes for having a port at all.

The two levels
--------------
Each stored credential is encrypted under its own freshly generated **data
key**, and only that data key is encrypted under the **master key**. ADR-070
rejects encrypting credentials directly with the master key because rotation
then becomes a migration over every ciphertext, with downtime and a
half-migrated state to reason about; here rotation re-wraps N small keys and
never touches a ciphertext.

:class:`MasterKeyProvider` therefore only ever sees a data key. It is never
passed a credential, and a provider that never sees a credential cannot leak
one.

Why the nonce is prefixed rather than stored in its own column
--------------------------------------------------------------
GCM needs a unique nonce per encryption, and there are two encryptions here --
wrapping the data key, and sealing the credential. ADR-070 fixes the row's
shape as "the ciphertext and the wrapped data key", two values, so the nonce
cannot have a column of its own without contradicting it. Each blob is instead
self-describing: ``nonce || ciphertext || tag``. That also keeps the nonce
physically inseparable from the ciphertext it belongs to, which is the failure
a separate column invites.

Nonces are random rather than counters. A counter is the stronger construction
where a single writer owns the sequence, but this vault is written by every
process that stores a credential, and a counter shared across processes is a
coordination problem that would need its own decision. At 96 bits and the
volumes a credential vault sees, random is sound.

Why a failed unwrap is a SafetyError
------------------------------------
GCM authenticates: a tampered, truncated, or foreign-master-key blob fails to
decrypt rather than yielding plausible garbage, which is exactly why ADR-070
requires authenticated encryption. The taxonomy (ADR-038) is closed, so this
raises a member of it rather than introducing a crypto-specific error.

:class:`~dhruva.shared.errors.SafetyError` is the honest member. The operation
was *refused because safety could not be established* -- the integrity of the
key material could not be shown -- which is ADR-022's fail-closed posture. It is
deliberately not :class:`~dhruva.shared.errors.ValidationError`, because the
input did not come from a caller who could correct it; it came from the
database, and a validation error would invite a retry with "better" input that
does not exist. It is deliberately not
:class:`~dhruva.shared.errors.ConfigurationError` either: a wrong master key
does produce this failure, but so does a tampered row, and GCM cannot tell the
two apart. Naming the cause would be a guess recorded as fact.

Known gap: no associated data
-----------------------------
GCM can bind a ciphertext to context through associated data, which would stop
a ciphertext being moved from one credential row to another and still
decrypting. Doing that requires a stable record identity to bind to, and the
credential table does not exist yet -- it is design §13 step 3. Adding a
made-up binding now would be schema invented ahead of its migration, so it is
recorded here and in the S06 design instead.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dhruva.contexts.platform.domain.identity.keys import KeyProvider
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import ConfigurationError, SafetyError

__all__ = [
    "DATA_KEY_BYTES",
    "MASTER_KEY_BYTES",
    "NONCE_BYTES",
    "EncryptedSecret",
    "MasterKeyProvider",
    "decrypt_secret",
    "encrypt_secret",
]

#: 96 bits, the nonce size GCM is specified for (NIST SP 800-38D). Other sizes
#: are legal and slower, because anything else is rehashed into 96 bits first.
NONCE_BYTES: Final = 12

#: AES-256 means a 256-bit key, for both levels of the hierarchy.
MASTER_KEY_BYTES: Final = 32
DATA_KEY_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """A sealed credential and the wrapped key that opens it (ADR-070).

    Exactly the two values ADR-070 says a row stores. Neither field is
    meaningful without a :class:`KeyProvider`, and neither reveals anything
    about the plaintext beyond its approximate length.

    Frozen because these two values are only correct together: a ciphertext
    paired with a re-wrapped key from another record decrypts to nothing, and
    making that unrepresentable is cheaper than testing for it.
    """

    ciphertext: bytes
    wrapped_data_key: bytes


def _decode_master_key(master_key: SecretValue) -> bytes:
    """Decode and check the configured master key.

    Raises
    ------
    ConfigurationError
        If the value is not base64, or does not decode to exactly
        :data:`MASTER_KEY_BYTES` bytes. Both are bootstrap failures and never
        retryable: the same configuration fails the same way.

    Notes
    -----
    The failure carries no part of the key, not even its length when the length
    is wrong -- ``expected`` is a constant and ``actual`` is a count, so a log
    record can say what to fix without becoming the leak it is reporting.
    """
    try:
        decoded = base64.b64decode(master_key.reveal(), validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = "credential vault master key is not valid base64"
        raise ConfigurationError(msg, field="crypto.master_key") from exc

    if len(decoded) != MASTER_KEY_BYTES:
        msg = "credential vault master key is the wrong length"
        raise ConfigurationError(
            msg,
            field="crypto.master_key",
            expected_bytes=MASTER_KEY_BYTES,
            actual_bytes=len(decoded),
        )

    return decoded


def _seal(cipher: AESGCM, plaintext: bytes) -> bytes:
    """Return ``nonce || ciphertext || tag`` under a fresh random nonce."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + cipher.encrypt(nonce, plaintext, None)


def _open(cipher: AESGCM, blob: bytes, *, what: str) -> bytes:
    """Reverse :func:`_seal`, refusing anything that does not authenticate.

    Raises
    ------
    SafetyError
        If ``blob`` is too short to contain a nonce, or fails authentication.
        Both are the same refusal from the caller's point of view, and both are
        deliberately indistinguishable in what they report.
    """
    if len(blob) <= NONCE_BYTES:
        msg = "encrypted value is truncated"
        raise SafetyError(msg, what=what)

    nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return cipher.decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        msg = "encrypted value failed authentication"
        raise SafetyError(msg, what=what) from exc


class MasterKeyProvider:
    """The :class:`KeyProvider` ADR-070 ships: wrapping backed by the master key.

    Satisfies the port structurally rather than by inheritance, matching how the
    platform's other adapters conform. A KMS-backed provider is a later
    substitution that changes nothing above this line.

    The master key is decoded once, at construction, so a misconfigured process
    fails at bootstrap rather than at the first credential read -- which in a
    trading system would be at the least convenient moment available.
    """

    __slots__ = ("_cipher",)

    def __init__(self, master_key: SecretValue) -> None:
        """Initialise the provider from configuration.

        Parameters
        ----------
        master_key
            Base64-encoded 32-byte key material, as
            :class:`~dhruva.shared.config.settings.CryptoSettings` supplies it.

        Raises
        ------
        ConfigurationError
            If the key is not base64 or is not 32 bytes once decoded.
        """
        self._cipher = AESGCM(_decode_master_key(master_key))

    def wrap_key(self, data_key: bytes) -> bytes:
        """Encrypt ``data_key`` under the master key."""
        return _seal(self._cipher, data_key)

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Recover a data key wrapped by :meth:`wrap_key`.

        Raises
        ------
        SafetyError
            If the wrapped key is truncated, tampered with, or was wrapped under
            a different master key.
        """
        return _open(self._cipher, wrapped_key, what="wrapped_data_key")


def encrypt_secret(plaintext: bytes, key_provider: KeyProvider) -> EncryptedSecret:
    """Seal ``plaintext`` under a fresh data key, wrapped by ``key_provider``.

    Parameters
    ----------
    plaintext
        The credential to store. Callers pass bytes rather than
        :class:`~dhruva.shared.config.secret.SecretValue` because what gets
        encrypted must be exact, and a redacting wrapper is the wrong shape for
        a value whose every byte matters.
    key_provider
        Wraps the generated data key. Taken as an argument rather than held,
        so that swapping in a KMS provider is a call-site change and not a
        change here.

    Returns
    -------
    EncryptedSecret
        The two values a row stores.

    Notes
    -----
    A new data key per call, never reused across credentials: reuse would make
    one compromised key open more than one record, and would put the pair
    (key, nonce) at risk of repeating, which is the one thing GCM must never do.
    """
    data_key = AESGCM.generate_key(bit_length=DATA_KEY_BYTES * 8)
    return EncryptedSecret(
        ciphertext=_seal(AESGCM(data_key), plaintext),
        wrapped_data_key=key_provider.wrap_key(data_key),
    )


def decrypt_secret(secret: EncryptedSecret, key_provider: KeyProvider) -> bytes:
    """Recover the plaintext of ``secret``.

    Separate from any repository read, and requiring ``key_provider``
    explicitly, because ADR-070 requires that no code path yields a plaintext
    without one. A plaintext returned by default is a plaintext that reaches a
    log line, an exception message or a debugger by accident; making the path
    explicit makes it greppable, which is the reasoning ADR-033 applied to
    :meth:`SecretValue.reveal`.

    Raises
    ------
    SafetyError
        If the wrapped key or the ciphertext fails authentication.
    """
    data_key = key_provider.unwrap_key(secret.wrapped_data_key)
    return _open(AESGCM(data_key), secret.ciphertext, what="ciphertext")
