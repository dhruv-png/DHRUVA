"""The local JWT adapter: what it signs, and what it refuses (ADR-072, ADR-011).

Three groups of test, and the middle one is the reason this file is long.

The round trip proves the adapter works. The **refusals** prove it cannot be
talked out of working -- an unsigned token, a token signed with another key, a
token whose algorithm the header claims is something else. Those are the attacks
a JWT verifier actually faces, and each is one library option away from
succeeding.

The clock tests prove ADR-011 holds where it is easiest to lose: PyJWT verifies
``exp`` against the wall clock by default, which would make this adapter's expiry
untestable without sleeping and wrong under replay.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import jwt
import pytest

from dhruva.contexts.platform.domain.identity import TokenClaims
from dhruva.contexts.platform.infrastructure.identity import (
    ALGORITHM,
    ISSUER,
    MINIMUM_KEY_BYTES,
    JwtTokenIssuer,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import AuthenticationError, ConfigurationError, TokenExpiredError
from dhruva.shared.identity import AccountId
from dhruva.shared.time import FrozenClock

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
NOW: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
EXPIRES: Final = NOW + timedelta(minutes=15)

#: Long enough to satisfy the minimum, fixed so a failure reproduces.
KEY_MATERIAL: Final = "k" * MINIMUM_KEY_BYTES
OTHER_KEY_MATERIAL: Final = "j" * MINIMUM_KEY_BYTES


@pytest.fixture
def issuer() -> JwtTokenIssuer:
    """Return an issuer bound to the test signing key."""
    return JwtTokenIssuer(SecretValue(KEY_MATERIAL, register=False))


@pytest.fixture
def claims() -> TokenClaims:
    """Return claims for a fifteen-minute access token (plan §15.1)."""
    return TokenClaims(subject="operator@dhruva.local", account_id=ACCOUNT, expires_at=EXPIRES)


def _clock(at: datetime) -> FrozenClock:
    return FrozenClock(at)


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


def test_minted_claims_survive_verification(issuer: JwtTokenIssuer, claims: TokenClaims) -> None:
    """Every field back exactly as asserted."""
    verified = issuer.verify(issuer.mint(claims), clock=_clock(NOW))

    assert verified.subject == claims.subject
    assert verified.account_id == claims.account_id
    assert verified.expires_at == claims.expires_at


def test_the_token_is_opaque_but_not_encrypted(issuer: JwtTokenIssuer, claims: TokenClaims) -> None:
    """A JWT is signed, not sealed, and this pins that nobody forgets it.

    Anybody holding the token can read its claims; the signature only stops them
    changing one. That is why no secret may ever be put in a claim, and why the
    subject and account -- both already known to the holder -- are all this
    carries.
    """
    payload = issuer.mint(claims).split(".")[1]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))

    assert decoded["sub"] == claims.subject
    assert set(decoded) == {"iss", "sub", "acc", "exp"}


# --------------------------------------------------------------------------- #
# The refusals. These are the attacks.
# --------------------------------------------------------------------------- #


def test_an_unsigned_token_is_refused(issuer: JwtTokenIssuer) -> None:
    """The ``alg: none`` attack, which pinning the algorithm list is what stops.

    A JWT carries its own algorithm in its header. A verifier that trusts that
    header can be handed a token claiming no algorithm at all -- unsigned, with
    whatever claims the attacker likes -- and will accept it. This is the single
    most important assertion in the file.
    """
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "attacker",
            "acc": str(ACCOUNT.value),
            "exp": int(EXPIRES.timestamp()),
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(AuthenticationError):
        issuer.verify(forged, clock=_clock(NOW))


def test_a_token_signed_with_another_key_is_refused(claims: TokenClaims) -> None:
    """The ordinary forgery, and the reason the key length is enforced."""
    other = JwtTokenIssuer(SecretValue(OTHER_KEY_MATERIAL, register=False))
    mine = JwtTokenIssuer(SecretValue(KEY_MATERIAL, register=False))

    with pytest.raises(AuthenticationError):
        mine.verify(other.mint(claims), clock=_clock(NOW))


def test_a_tampered_payload_is_refused(issuer: JwtTokenIssuer, claims: TokenClaims) -> None:
    """Editing a claim invalidates the signature, which is the whole mechanism."""
    header, _payload, signature = issuer.mint(claims).split(".")
    swapped = base64.urlsafe_b64encode(
        json.dumps(
            {"iss": ISSUER, "sub": "someone-else", "acc": str(ACCOUNT.value), "exp": 9999999999}
        ).encode()
    ).rstrip(b"=")

    with pytest.raises(AuthenticationError):
        issuer.verify(f"{header}.{swapped.decode()}.{signature}", clock=_clock(NOW))


@pytest.mark.parametrize(
    "malformed",
    ["", "not-a-token", "a.b", "a.b.c.d", "eyJhbGciOiJIUzI1NiJ9..", "..."],
)
def test_a_malformed_token_is_refused(issuer: JwtTokenIssuer, malformed: str) -> None:
    """Structural nonsense produces the same error as a forgery.

    Deliberately the same: the difference between "this is not a JWT" and "this
    JWT is not yours" is useful only to somebody probing the verifier.
    """
    with pytest.raises(AuthenticationError):
        issuer.verify(malformed, clock=_clock(NOW))


def test_a_token_from_another_issuer_is_refused(issuer: JwtTokenIssuer) -> None:
    """The ``iss`` claim is verified, so a token minted elsewhere is not ours."""
    foreign = jwt.encode(
        {
            "iss": "somebody-else",
            "sub": "operator@dhruva.local",
            "acc": str(ACCOUNT.value),
            "exp": int(EXPIRES.timestamp()),
        },
        KEY_MATERIAL,
        algorithm=ALGORITHM,
    )

    with pytest.raises(AuthenticationError):
        issuer.verify(foreign, clock=_clock(NOW))


@pytest.mark.parametrize("missing", ["iss", "sub", "acc", "exp"])
def test_a_token_missing_a_required_claim_is_refused(issuer: JwtTokenIssuer, missing: str) -> None:
    """A token without ``exp`` would otherwise verify and never expire."""
    payload = {
        "iss": ISSUER,
        "sub": "operator@dhruva.local",
        "acc": str(ACCOUNT.value),
        "exp": int(EXPIRES.timestamp()),
    }
    del payload[missing]

    with pytest.raises(AuthenticationError):
        issuer.verify(jwt.encode(payload, KEY_MATERIAL, algorithm=ALGORITHM), clock=_clock(NOW))


def test_a_non_numeric_expiry_is_refused(issuer: JwtTokenIssuer) -> None:
    """A string ``exp`` parses happily and then compares as never-expiring.

    Which is the sort of defect that surfaces only once somebody is exploiting
    it, so it is refused rather than coerced.
    """
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "x", "acc": str(ACCOUNT.value), "exp": "not-a-time"},
        KEY_MATERIAL,
        algorithm=ALGORITHM,
    )

    with pytest.raises(AuthenticationError):
        issuer.verify(forged, clock=_clock(NOW))


def test_a_malformed_account_claim_is_refused(issuer: JwtTokenIssuer) -> None:
    """Bad data off the wire, not a programming error, so not an invariant violation."""
    forged = jwt.encode(
        {"iss": ISSUER, "sub": "x", "acc": "NIFTY", "exp": int(EXPIRES.timestamp())},
        KEY_MATERIAL,
        algorithm=ALGORITHM,
    )

    with pytest.raises(AuthenticationError):
        issuer.verify(forged, clock=_clock(NOW))


# --------------------------------------------------------------------------- #
# Expiry is the injected clock's, never the wall clock's (ADR-011)
# --------------------------------------------------------------------------- #


def test_an_expired_token_is_refused_according_to_the_injected_clock(
    issuer: JwtTokenIssuer, claims: TokenClaims
) -> None:
    """The check PyJWT would have done against ``time.time()``.

    Doing it here instead is what makes this assertion possible without sleeping
    for fifteen minutes, and what makes a replay reproducible (ADR-069).
    """
    token = issuer.mint(claims)

    with pytest.raises(TokenExpiredError):
        issuer.verify(token, clock=_clock(EXPIRES + timedelta(seconds=1)))


def test_a_token_is_valid_up_to_and_including_its_expiry(
    issuer: JwtTokenIssuer, claims: TokenClaims
) -> None:
    """Boundary-inclusive, matching ``RefreshToken.is_expired``.

    The two carry the same rule and would be a quiet inconsistency if they
    disagreed by one instant.
    """
    verified = issuer.verify(issuer.mint(claims), clock=_clock(EXPIRES))

    assert verified.subject == claims.subject


def test_expiry_does_not_consult_the_wall_clock(
    issuer: JwtTokenIssuer, claims: TokenClaims
) -> None:
    """A token minted for 2026 verifies under a clock frozen in 2026.

    Under a wall-clock check this test's outcome would depend on the date the
    suite runs, which is the property ADR-011 exists to remove. It passes today
    and in ten years.
    """
    long_ago = TokenClaims(
        subject=claims.subject,
        account_id=ACCOUNT,
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    verified = issuer.verify(
        issuer.mint(long_ago), clock=_clock(datetime(2019, 12, 31, tzinfo=UTC))
    )

    assert verified.expires_at == long_ago.expires_at


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #


def test_a_short_signing_key_is_refused_at_construction() -> None:
    """HS256's security rests entirely on the key, and the token is given away.

    A short key is brute-forceable offline by anyone holding a single token --
    and by design every client holds one. Failing at startup means a
    misconfigured deployment stops before it mints anything, rather than at
    somebody's first login.
    """
    with pytest.raises(ConfigurationError):
        JwtTokenIssuer(SecretValue("too-short", register=False))


def test_a_key_of_exactly_the_minimum_is_accepted(claims: TokenClaims) -> None:
    """The boundary, so the check cannot drift to off-by-one without notice.

    Asserted by minting rather than by the constructor's truthiness: a key one
    byte over the limit that constructed but could not sign would pass the
    weaker check and fail at the first login.
    """
    issuer = JwtTokenIssuer(SecretValue("k" * MINIMUM_KEY_BYTES, register=False))

    assert issuer.mint(claims)
