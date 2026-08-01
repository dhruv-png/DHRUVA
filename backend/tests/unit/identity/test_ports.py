"""The identity ports are abstract and import nothing concrete (ADR-070, ADR-072).

The most valuable assertion here is the last one: that these modules import no
cryptographic and no JWT library. Nothing else in the repository enforces it --
boundary rule R7 covers persistence and R9 covers transports, and neither covers
a cipher -- so until a rule R10 exists, this test is the enforcement.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from dhruva.contexts.platform.domain import identity
from dhruva.contexts.platform.domain.identity import KeyProvider, TokenClaims, TokenIssuer
from dhruva.shared.identity import AccountId
from dhruva.shared.time import Clock, FrozenClock

pytestmark = pytest.mark.unit

ACCOUNT = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
EXPIRES = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)

#: Modules whose imports are policed below.
PORT_MODULES = ("keys.py", "tokens.py")

#: Any of these appearing in a port module means the domain has grown a
#: dependency on how a secret is actually protected (ADR-070, ADR-072).
FORBIDDEN_IMPORTS = frozenset(
    {"cryptography", "jwt", "pyjwt", "jose", "argon2", "passlib", "hashlib", "secrets", "hmac"}
)


class StubKeyProvider:
    """Conforms structurally, wraps nothing. Proves the port is satisfiable."""

    def wrap_key(self, data_key: bytes) -> bytes:
        """Return a recognisably wrapped form, so a test can tell them apart."""
        return b"wrapped:" + data_key

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        """Reverse :meth:`wrap_key`."""
        return wrapped_key.removeprefix(b"wrapped:")


class StubTokenIssuer:
    """Conforms structurally, signs nothing."""

    def mint(self, claims: TokenClaims) -> str:
        """Return a token naming its subject, so a test can read it back."""
        return f"token:{claims.subject}"

    def verify(self, token: str, *, clock: Clock) -> TokenClaims:
        """Use the injected clock, so the port's signature is exercised not ignored."""
        return TokenClaims(
            subject=token.removeprefix("token:"),
            account_id=ACCOUNT,
            expires_at=max(EXPIRES, clock.now()),
        )


def test_a_conforming_object_satisfies_the_key_provider_port() -> None:
    """Structural typing, so an adapter need not import the port to implement it."""
    assert isinstance(StubKeyProvider(), KeyProvider)


def test_a_conforming_object_satisfies_the_token_issuer_port() -> None:
    """The same structural check for the second port."""
    assert isinstance(StubTokenIssuer(), TokenIssuer)


def test_an_object_missing_a_method_does_not_satisfy_the_port() -> None:
    """Asserted so the previous two tests are known to discriminate."""

    class Incomplete:
        def wrap_key(self, data_key: bytes) -> bytes:
            return data_key

    assert not isinstance(Incomplete(), KeyProvider)


def test_verification_takes_a_clock_rather_than_reading_one() -> None:
    """ADR-011: expiry is evaluated against injected time, never the wall clock."""
    claims = StubTokenIssuer().verify("token:operator", clock=FrozenClock(EXPIRES))

    assert claims.subject == "operator"


def test_claims_cannot_be_edited_after_verification() -> None:
    """A mutable claim set is one the signature no longer covers."""
    claims = TokenClaims(subject="operator", account_id=ACCOUNT, expires_at=EXPIRES)

    with pytest.raises(dataclasses.FrozenInstanceError):
        claims.subject = "someone_else"  # type: ignore[misc]


def test_the_ports_import_no_cryptographic_or_token_library() -> None:
    """The reason these are ports at all (ADR-070, ADR-072).

    Parsed rather than grepped, so a name inside a docstring or a comment does
    not fail the build and an aliased import cannot pass it.
    """
    root = Path(identity.__file__).parent
    offenders: list[str] = []
    for name in PORT_MODULES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{name}: {r}" for r in roots if r in FORBIDDEN_IMPORTS]

    assert not offenders, f"a domain port grew a concrete dependency: {offenders}"
