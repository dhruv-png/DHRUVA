"""Identity and authorisation. Pure domain logic, no persistence and no I/O.

Ports, policy and aggregates (ADR-070, ADR-072, ADR-073). Nothing here performs
cryptography, signs a token, hashes a password, or touches a database -- the
credential aggregate holds sealed bytes and has no way to open them, the
principal holds a sealed second factor and a one-way hash and can do nothing with
either, and the refresh token holds a digest rather than the token it stands for.

What *is* here is the reasoning: what a valid principal is, what presenting a
refresh token means, and which of those meanings is evidence of theft.
"""

from __future__ import annotations

from dhruva.contexts.platform.domain.identity.authorisation import (
    TWO_FACTOR_REQUIRED,
    GrantRefusal,
    GrantVerdict,
    Permission,
    PermissionGrant,
    Role,
    is_permitted,
    may_grant,
)
from dhruva.contexts.platform.domain.identity.credentials import (
    BROKER_MAX_LENGTH,
    Credential,
    EncryptedSecret,
    credential_associated_data,
)
from dhruva.contexts.platform.domain.identity.keys import KeyProvider
from dhruva.contexts.platform.domain.identity.passwords import (
    REDACTED_HASH,
    PasswordHash,
    PasswordHasher,
)
from dhruva.contexts.platform.domain.identity.ports import (
    AuditSink,
    AuthorisationDirectory,
    IdentityUnitOfWork,
    PrincipalStore,
    RefreshTokenStore,
    RoleStore,
)
from dhruva.contexts.platform.domain.identity.principals import Principal
from dhruva.contexts.platform.domain.identity.refresh import RefreshToken, RefreshVerdict
from dhruva.contexts.platform.domain.identity.tokens import TokenClaims, TokenIssuer

__all__ = [
    "BROKER_MAX_LENGTH",
    "REDACTED_HASH",
    "TWO_FACTOR_REQUIRED",
    "AuditSink",
    "AuthorisationDirectory",
    "Credential",
    "EncryptedSecret",
    "GrantRefusal",
    "GrantVerdict",
    "IdentityUnitOfWork",
    "KeyProvider",
    "PasswordHash",
    "PasswordHasher",
    "Permission",
    "PermissionGrant",
    "Principal",
    "PrincipalStore",
    "RefreshToken",
    "RefreshTokenStore",
    "RefreshVerdict",
    "Role",
    "RoleStore",
    "TokenClaims",
    "TokenIssuer",
    "credential_associated_data",
    "is_permitted",
    "may_grant",
]
