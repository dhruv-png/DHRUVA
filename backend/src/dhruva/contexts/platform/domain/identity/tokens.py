"""The TokenIssuer port (ADR-072, ADR-011).

Abstract only. No JWT library is imported here, and none may be: ADR-072 makes
"OIDC-ready" a commitment, and that is only cheap if exactly one module knows how
a token is minted. The adapter that signs locally and an OIDC provider are peers
behind this port.

Why verification takes a Clock
------------------------------
ADR-011 injects time; nothing reads the wall clock. An expiry check against
``datetime.now()`` would be untestable without sleeping and unreproducible in a
replay, and ADR-072 records clock skew as a live correctness concern. Passing the
clock makes the dependency visible at the call site rather than hidden in an
adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import AccountId
    from dhruva.shared.time import Clock

__all__ = ["TokenClaims", "TokenIssuer"]


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """What a verified access token asserts.

    Frozen: claims are a statement already made and signed. A mutable claim set
    invites a caller to "correct" one after verification, at which point the
    signature no longer covers what the code believes.

    Attributes
    ----------
    subject
        Stable identifier of the principal. A ``str`` for the reason recorded on
        ``AuditRecord.actor``: ADR-072 does not settle whether the platform
        authenticates humans only or services too.
    account_id
        The tenant the token acts within (ADR-004).
    expires_at
        When the token stops being valid. Timezone-aware, always (ADR-006).
    """

    subject: str
    account_id: AccountId
    expires_at: datetime


@runtime_checkable
class TokenIssuer(Protocol):
    """Mints and verifies access tokens (ADR-072)."""

    def mint(self, claims: TokenClaims) -> str:
        """Return a signed token asserting ``claims``.

        The 15-minute lifetime plan §15.1 fixes is applied by the caller that
        builds ``claims``, not here: an issuer that invented its own expiry would
        make the policy invisible at the point somebody reads the call.
        """
        ...

    def verify(self, token: str, *, clock: Clock) -> TokenClaims:
        """Verify ``token`` and return its claims.

        Verification is signature and expiry only -- no database lookup, per
        ADR-072's statelessness trade. Expiry is evaluated against ``clock``
        (ADR-011), never the wall clock.

        Raises
        ------
        Exception
            Implementations raise from the closed taxonomy (ADR-038) when the
            signature is invalid or the token has expired.
        """
        ...
