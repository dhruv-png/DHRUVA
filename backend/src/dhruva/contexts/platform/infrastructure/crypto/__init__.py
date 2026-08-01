"""Cryptographic adapters. The cipher lives here and nowhere else.

The S06 dependency plan permits ``cryptography`` in this package and forbids it
in every ``domain`` and ``application`` module. That separation is currently a
convention rather than a build failure: boundary rule R7 covers persistence
frameworks and R9 covers transports, and neither covers a cipher. Rule R10 would
close it and is deferred as an ADR-level change, so until it exists the
enforcement is the ast-parsing tests over the identity modules.

:class:`EncryptedSecret` is re-exported rather than defined here. It moved down
to ``domain.identity.credentials`` when the credential aggregate arrived, since
a domain object holding an infrastructure class is the import the layer contract
exists to fail. The name is kept available from this package so that callers who
already reach for it beside the cipher still find it.
"""

from __future__ import annotations

from dhruva.contexts.platform.infrastructure.crypto.credentials import (
    open_credential,
    seal_credential,
)
from dhruva.contexts.platform.infrastructure.crypto.envelope import (
    DATA_KEY_BYTES,
    MASTER_KEY_BYTES,
    NONCE_BYTES,
    EncryptedSecret,
    MasterKeyProvider,
    decrypt_secret,
    encrypt_secret,
)

__all__ = [
    "DATA_KEY_BYTES",
    "MASTER_KEY_BYTES",
    "NONCE_BYTES",
    "EncryptedSecret",
    "MasterKeyProvider",
    "decrypt_secret",
    "encrypt_secret",
    "open_credential",
    "seal_credential",
]
