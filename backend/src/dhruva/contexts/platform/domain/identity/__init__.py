"""Identity and authorisation. Pure domain logic, no persistence and no I/O.

Three ports and one policy (ADR-070, ADR-072, ADR-073). Nothing here performs
cryptography, signs a token, or touches a database.
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
from dhruva.contexts.platform.domain.identity.keys import KeyProvider
from dhruva.contexts.platform.domain.identity.tokens import TokenClaims, TokenIssuer

__all__ = [
    "TWO_FACTOR_REQUIRED",
    "GrantRefusal",
    "GrantVerdict",
    "KeyProvider",
    "Permission",
    "Role",
    "TokenClaims",
    "TokenIssuer",
    "is_permitted",
    "may_grant",
]
