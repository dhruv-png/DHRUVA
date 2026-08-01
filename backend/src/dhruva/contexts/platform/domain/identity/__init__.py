"""Identity and authorisation. Pure domain logic, no persistence and no I/O.

Three ports, one policy and one aggregate (ADR-070, ADR-072, ADR-073). Nothing
here performs cryptography, signs a token, or touches a database -- the
credential aggregate holds sealed bytes and has no way to open them.
"""

from __future__ import annotations

from dhruva.contexts.platform.domain.identity.authorisation import (
    TWO_FACTOR_REQUIRED,
    GrantRefusal,
    GrantVerdict,
    Permission,
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
from dhruva.contexts.platform.domain.identity.tokens import TokenClaims, TokenIssuer

__all__ = [
    "BROKER_MAX_LENGTH",
    "TWO_FACTOR_REQUIRED",
    "Credential",
    "EncryptedSecret",
    "GrantRefusal",
    "GrantVerdict",
    "KeyProvider",
    "Permission",
    "Role",
    "TokenClaims",
    "TokenIssuer",
    "credential_associated_data",
    "is_permitted",
    "may_grant",
]
