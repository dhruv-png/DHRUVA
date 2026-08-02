"""Sealing and opening a broker credential (ADR-070, ADR-033, ADR-037).

Two functions, and they are the entire plaintext surface of the credential
store. ADR-070 requires that no code path yields a plaintext without a
``KeyProvider``; keeping that path in one small named module is what makes the
requirement checkable by reading, and greppable by anyone reviewing where
secrets travel.

Why this is not in ``envelope.py``
----------------------------------
``envelope.py`` knows about ciphers and knows nothing about credentials, which
is why it can be read and reviewed as cryptography. Teaching it what a
credential is would mean the module that must be right about GCM also has to be
right about the record shape. These two functions are the join, and they hold
no key material of their own.

Why the boundary is text
------------------------
``encrypt_secret`` deals in bytes on purpose -- every byte of a secret matters,
and a redacting wrapper is the wrong shape for that. One layer up, a broker
credential *is* text: an API key, a token. Taking and returning
:class:`~dhruva.shared.config.secret.SecretValue` here means an opened
credential arrives already registered for log redaction (ADR-037), rather than
as a bare ``str`` that the logging stack has no way to recognise.

The UTF-8 round trip is total for anything sealed through :func:`seal_credential`,
because a Python ``str`` always encodes and decodes back unchanged. It is only
reachable by sealing arbitrary bytes through ``encrypt_secret`` directly and
then storing them as a credential -- a bypass, reported as a data-quality fault
rather than pretended away with ``errors="replace"``, which would silently hand
back a corrupted secret.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.platform.domain.identity.credentials import credential_associated_data
from dhruva.contexts.platform.infrastructure.crypto.envelope import decrypt_secret, encrypt_secret
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import DataQualityError

if TYPE_CHECKING:
    from dhruva.contexts.platform.domain.identity.credentials import Credential, EncryptedSecret
    from dhruva.contexts.platform.domain.identity.keys import KeyProvider
    from dhruva.shared.identity import AccountId, CredentialId

__all__ = ["open_credential", "seal_credential"]


def seal_credential(
    secret: SecretValue,
    key_provider: KeyProvider,
    *,
    credential_id: CredentialId,
    account_id: AccountId,
    broker: str,
) -> EncryptedSecret:
    """Seal ``secret`` bound to the record it will occupy (TD-S06-6).

    The three binding facts are taken as keyword arguments rather than read off
    a :class:`~dhruva.contexts.platform.domain.identity.credentials.Credential`,
    because on the write path the credential does not exist yet -- it cannot be
    constructed until there is sealed material to put in it.

    Parameters
    ----------
    secret
        The plaintext credential.
    key_provider
        Wraps the freshly generated data key (ADR-070).
    credential_id, account_id, broker
        The record this ciphertext may be opened from, and no other.

    Returns
    -------
    EncryptedSecret
        Ciphertext and wrapped data key, ready to store.
    """
    return encrypt_secret(
        secret.reveal().encode(),
        key_provider,
        associated_data=credential_associated_data(credential_id, account_id, broker),
    )


def open_credential(credential: Credential, key_provider: KeyProvider) -> SecretValue:
    """Recover the plaintext of ``credential``.

    The one call in the system that turns a stored credential into a usable
    secret. It takes the whole aggregate rather than its sealed field so the
    associated data is derived from the record itself and cannot be passed
    wrongly -- a caller cannot open row A's ciphertext under row B's binding,
    because there is no argument through which to try.

    Parameters
    ----------
    credential
        The credential as read from the store.
    key_provider
        Unwraps the data key. Injected, never held: ADR-070 requires that
        obtaining a plaintext takes one explicitly.

    Returns
    -------
    SecretValue
        Registered for value-based log redaction (ADR-037), so a later
        accidental interpolation into a log line is masked.

    Raises
    ------
    SafetyError
        If the wrapped key or the ciphertext fails authentication -- including
        an intact ciphertext that belongs to a different record, which is the
        tampering TD-S06-6 exists to refuse.
    DataQualityError
        If the decrypted bytes are not UTF-8. Unreachable for anything sealed by
        :func:`seal_credential`; see the module docstring.
    """
    plaintext = decrypt_secret(
        credential.secret, key_provider, associated_data=credential.associated_data
    )
    try:
        return SecretValue(plaintext.decode())
    except UnicodeDecodeError as exc:
        # The failure reports the credential, never the bytes: a partially
        # decoded secret in an error message is still a secret.
        msg = "stored credential is not valid UTF-8"
        raise DataQualityError(
            msg, credential_id=str(credential.credential_id), broker=credential.broker
        ) from exc
