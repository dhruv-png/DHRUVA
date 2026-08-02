"""The local JWT adapter (ADR-072, ADR-011).

The only module in the platform that imports a JWT library, which is what makes
"OIDC-ready" cheap: an external provider is a second implementation of
:class:`~dhruva.contexts.platform.domain.identity.TokenIssuer`, and no caller
changes.

Why HS256
---------
Symmetric signing, because today exactly one process both mints and verifies. An
asymmetric algorithm exists so that a *verifier* can check a signature without
being able to forge one, which matters when those are different parties -- and
when they become different parties, that is an OIDC provider and this adapter is
replaced rather than upgraded. Choosing RS256 now would buy a property nothing
uses and add key-pair custody to a subsystem that already has two keys to manage.

Why the algorithm is pinned on the way in
-----------------------------------------
:func:`jwt.decode` is given an explicit ``algorithms`` list, and that is a
security control rather than a formality. A JWT carries its own algorithm in its
header, so a verifier that trusts that header can be handed a token claiming
``alg: none`` -- unsigned, and accepted. The pinned list means the header is
checked against what this platform actually uses, and anything else is refused
before the signature is even considered. A test asserts it.

Why expiry is not left to the library
-------------------------------------
PyJWT verifies ``exp`` against :func:`time.time`, which is the wall clock.
ADR-011 says nothing in this platform reads the wall clock: an expiry evaluated
that way is untestable without sleeping and wrong under replay, and ADR-072
records clock skew as a live correctness concern precisely because this check is
the one that moves. So the library's own expiry verification is **disabled**, and
the claim is compared against the injected :class:`~dhruva.shared.time.Clock`.

The rest of the library's verification -- signature, structure, algorithm -- is
left exactly where it is. This overrides one check for a stated reason; it does
not reimplement JWT.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import jwt

from dhruva.contexts.platform.domain.identity.tokens import TokenClaims
from dhruva.shared.errors import AuthenticationError, ConfigurationError, TokenExpiredError
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.time import Clock

__all__ = ["ALGORITHM", "ISSUER", "JwtTokenIssuer"]

#: The one algorithm this platform mints and the only one it will verify.
ALGORITHM: Final = "HS256"

#: The ``iss`` claim. Constant rather than configurable: it identifies the
#: software that issued the token, not the deployment that ran it, and a
#: per-environment value would make a token minted in staging structurally
#: indistinguishable from one minted in production only by its signature.
ISSUER: Final = "dhruva"

#: Minimum signing key length, in bytes. HS256 is HMAC-SHA256, whose security
#: rests entirely on the key: a short one is brute-forceable offline by anyone
#: holding a single token, and the token is by design given to the client.
#: Refused at construction rather than at first use, so a misconfigured
#: deployment fails at startup instead of at somebody's first login.
MINIMUM_KEY_BYTES: Final = 32


class JwtTokenIssuer:
    """Mints and verifies HS256 access tokens.

    Implements :class:`~dhruva.contexts.platform.domain.identity.TokenIssuer`
    structurally; the protocol is not inherited, so nothing here imports the
    domain's abstraction in order to satisfy it.
    """

    __slots__ = ("_key",)

    def __init__(self, signing_key: SecretValue) -> None:
        """Bind the issuer to its signing key.

        Raises
        ------
        ConfigurationError
            If the key is too short to be safe. Raised here rather than at first
            use, because a deployment defect should stop a process at startup
            and not at somebody's first login -- and because it is a defect
            rather than an authentication outcome.
        """
        material = signing_key.reveal()
        if len(material.encode("utf-8")) < MINIMUM_KEY_BYTES:
            msg = "token signing key is too short to be safe"
            raise ConfigurationError(
                msg,
                field="auth.signing_key",
                minimum_bytes=MINIMUM_KEY_BYTES,
            )
        self._key = material

    def mint(self, claims: TokenClaims) -> str:
        """Return a signed token asserting ``claims``.

        The lifetime is the caller's: ``claims.expires_at`` arrives already
        decided, because an issuer that invented its own expiry would make plan
        §15.1's fifteen minutes invisible at the point somebody reads the call.
        """
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "sub": claims.subject,
            "acc": str(claims.account_id.value),
            "exp": int(claims.expires_at.timestamp()),
        }
        return jwt.encode(payload, self._key, algorithm=ALGORITHM)

    def verify(self, token: str, *, clock: Clock) -> TokenClaims:
        """Verify ``token`` and return its claims.

        Raises
        ------
        TokenExpiredError
            If ``exp`` has passed according to ``clock``.
        AuthenticationError
            If the signature is wrong, the algorithm is not the pinned one, the
            structure is malformed, or a required claim is missing. All four
            produce the same error deliberately: the differences are useful only
            to somebody probing the verifier.
        """
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],
                issuer=ISSUER,
                # Disabled here and performed below against the injected clock.
                # See the module docstring: this is the one check that must not
                # read the wall clock.
                options={"verify_exp": False, "require": ["iss", "sub", "acc", "exp"]},
            )
        except jwt.InvalidTokenError as error:
            msg = "token could not be verified"
            raise AuthenticationError(msg) from error

        expires_at = self._expiry(payload)
        if clock.now() > expires_at:
            msg = "token has expired"
            raise TokenExpiredError(msg, expires_at=expires_at.isoformat())

        return TokenClaims(
            subject=str(payload["sub"]),
            account_id=self._account(payload),
            expires_at=expires_at,
        )

    @staticmethod
    def _expiry(payload: dict[str, Any]) -> datetime:
        """Read ``exp`` as a timezone-aware instant.

        ``require`` above guarantees the claim is present; this guarantees it is
        a number that means a time. A token whose ``exp`` is a string parses
        happily and then compares as never-expiring, which is the kind of defect
        that only shows up once somebody is exploiting it.
        """
        try:
            return datetime.fromtimestamp(float(payload["exp"]), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError) as error:
            msg = "token could not be verified"
            raise AuthenticationError(msg) from error

    @staticmethod
    def _account(payload: dict[str, Any]) -> AccountId:
        """Read ``acc`` as an account identifier.

        A malformed identifier is an unverifiable token rather than an invariant
        violation: the value came off the wire, so the caller supplied bad data
        and did not make a programming error.
        """
        try:
            return AccountId(UUID(str(payload["acc"])))
        except (ValueError, AttributeError, TypeError) as error:
            msg = "token could not be verified"
            raise AuthenticationError(msg) from error
