"""Cryptographic adapters. The cipher lives here and nowhere else.

The S06 dependency plan permits ``cryptography`` in this package and forbids it
in every ``domain`` and ``application`` module. That separation is currently a
convention rather than a build failure: boundary rule R7 covers persistence
frameworks and R9 covers transports, and neither covers a cipher. Rule R10 would
close it and is deferred as an ADR-level change, so until it exists the
enforcement is the ast-parsing test over the identity ports.
"""

from __future__ import annotations

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
]
