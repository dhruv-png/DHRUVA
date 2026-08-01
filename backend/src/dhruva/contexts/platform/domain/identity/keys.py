"""The KeyProvider port (ADR-070, ADR-020).

Abstract only. This module names an operation; it does not perform one, and it
imports no cryptographic library. ADR-070 requires that the domain not know
whether a data key is unwrapped locally or by a KMS -- ADR-020 sanctions both by
writing "environment/KMS" -- and a domain that imported a cipher would have made
that choice by accident.

Why a data key and not the secret itself
----------------------------------------
Envelope encryption, per ADR-020: each stored credential gets its own data key,
and the master key encrypts only that. So the port wraps and unwraps a *key*,
never a credential. A provider that never sees a credential cannot leak one, and
rotating the master key re-wraps small keys rather than re-encrypting every
secret.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["KeyProvider"]


@runtime_checkable
class KeyProvider(Protocol):
    """Wraps and unwraps a per-record data key (ADR-070).

    Structural rather than inherited, matching how the platform's other ports
    are declared: an adapter satisfies this by having the methods, so a KMS
    client wrapper need not import this module to conform.
    """

    def wrap_key(self, data_key: bytes) -> bytes:
        """Encrypt ``data_key`` with the master key and return the wrapped form.

        The wrapped form is what a record stores. Implementations must not
        return the input, and must not log either value.
        """
        ...

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Recover the data key from its wrapped form.

        Raises
        ------
        Exception
            Implementations raise from the closed taxonomy (ADR-038) when the
            wrapped key is corrupt, truncated or was wrapped under a different
            master key. Authenticated encryption makes tampering a decryption
            failure rather than silent garbage, which is why ADR-070 requires it.
        """
        ...
