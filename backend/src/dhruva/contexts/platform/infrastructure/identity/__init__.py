"""Identity adapters: the only modules that know how a token or a hash is made.

Two libraries are imported in this package and nowhere else in the platform --
PyJWT in :mod:`~.tokens` and argon2-cffi in :mod:`~.passwords`. That containment
is the point of the ports in ``domain.identity``: ADR-072 makes "OIDC-ready" a
commitment, and it is only cheap while exactly one module knows how a token is
minted.
"""

from __future__ import annotations

from dhruva.contexts.platform.infrastructure.identity.metrics import (
    AUTHENTICATION_METRIC,
    AUTHORISATION_METRIC,
    PrometheusIdentityMetrics,
)
from dhruva.contexts.platform.infrastructure.identity.passwords import Argon2PasswordHasher
from dhruva.contexts.platform.infrastructure.identity.refresh_tokens import (
    TOKEN_ENTROPY_BYTES,
    Sha256RefreshTokenMinter,
)
from dhruva.contexts.platform.infrastructure.identity.tokens import (
    ALGORITHM,
    ISSUER,
    MINIMUM_KEY_BYTES,
    JwtTokenIssuer,
)

__all__ = [
    "ALGORITHM",
    "AUTHENTICATION_METRIC",
    "AUTHORISATION_METRIC",
    "ISSUER",
    "MINIMUM_KEY_BYTES",
    "TOKEN_ENTROPY_BYTES",
    "Argon2PasswordHasher",
    "JwtTokenIssuer",
    "PrometheusIdentityMetrics",
    "Sha256RefreshTokenMinter",
]
