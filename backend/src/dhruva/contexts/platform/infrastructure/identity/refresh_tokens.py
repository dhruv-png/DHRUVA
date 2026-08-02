"""Generating refresh tokens and digesting presented ones (ADR-072, ADR-033).

Why SHA-256 here and argon2 for passwords
-----------------------------------------
The same repository hashes two credentials with two algorithms, and the
difference is not an inconsistency -- it is what each algorithm is for.

Argon2's cost exists to defeat a **dictionary attack on a low-entropy secret**.
People choose passwords from a space small enough to enumerate, so the defence is
to make each guess expensive.

A refresh token is not chosen by anybody. It is 256 bits from the operating
system's CSPRNG, and there is no dictionary to run against it: an attacker
holding the entire table cannot enumerate that space at any cost per guess. So
argon2's work factor would buy nothing, and it would be charged on every refresh
-- roughly a hundred milliseconds added to a request that happens on a fixed
schedule for every active session, forever.

SHA-256 is the right tool for the actual requirement, which is "given a presented
token, find its row, without the row containing anything that could be presented".

Why the digest is compared by lookup rather than by comparison
--------------------------------------------------------------
The token is found with ``WHERE token_hash = :digest`` rather than by reading a
row and comparing bytes in Python, so there is no string comparison whose timing
could leak a prefix. Postgres's index lookup is not constant-time either, but it
is not comparing against a secret the attacker is trying to guess a character at
a time -- the attacker would have to produce 256 bits of preimage to get a hit at
all.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Final

from dhruva.contexts.platform.domain.identity.ports import MintedRefreshToken
from dhruva.shared.config.secret import SecretValue

__all__ = ["TOKEN_ENTROPY_BYTES", "Sha256RefreshTokenMinter"]

#: Entropy per refresh token, in bytes. 32 bytes is 256 bits, which is beyond
#: brute force and matches the digest width -- there is no point generating more
#: entropy than the hash can distinguish.
TOKEN_ENTROPY_BYTES: Final = 32


class Sha256RefreshTokenMinter:
    """Generates refresh tokens and digests them with SHA-256.

    Implements
    :class:`~dhruva.contexts.platform.domain.identity.ports.RefreshTokenMinter`
    structurally. Stateless, so one instance is safely shared.
    """

    __slots__ = ()

    def mint(self) -> MintedRefreshToken:
        """Generate a new refresh token and its digest.

        The secret comes back wrapped in a
        :class:`~dhruva.shared.config.secret.SecretValue`, registered for
        redaction (ADR-037), because between this line and the client's cookie it
        passes through a use case, a response and any log line either of them
        emits.
        """
        token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
        return MintedRefreshToken(secret=SecretValue(token), digest=_digest(token))

    def digest(self, presented: SecretValue) -> bytes:
        """Return the digest of a token a client presented.

        Must agree with :meth:`mint` byte for byte. A disagreement is not a
        subtle failure: every session in the platform would stop refreshing at
        once, which is at least loud.
        """
        return _digest(presented.reveal())


def _digest(token: str) -> bytes:
    """Hash a token to the bytes stored in ``refresh_token.token_hash``.

    UTF-8 explicitly rather than by platform default, so that the digest of a
    token does not depend on the machine that computed it.
    """
    return hashlib.sha256(token.encode("utf-8")).digest()
