"""The argon2 adapter: one-way, salted, and quiet about failures (ADR-072, ADR-033).

The leak test at the bottom is the one that matters most. Everything above it
asserts the hasher works; that one asserts that when it fails, and when its
output travels, nothing useful goes with it.
"""

from __future__ import annotations

from typing import Final

import pytest

from dhruva.contexts.platform.domain.identity import PasswordHash
from dhruva.contexts.platform.infrastructure.identity import Argon2PasswordHasher
from dhruva.shared.config.secret import SecretValue

pytestmark = pytest.mark.unit

PASSWORD: Final = "correct horse battery staple"
WRONG: Final = "Correct Horse Battery Staple"


@pytest.fixture(scope="module")
def hasher() -> Argon2PasswordHasher:
    """Return a hasher. Module-scoped because argon2 is deliberately slow."""
    return Argon2PasswordHasher()


def _secret(value: str) -> SecretValue:
    return SecretValue(value, register=False)


# --------------------------------------------------------------------------- #
# Hashing and verification
# --------------------------------------------------------------------------- #


def test_a_password_verifies_against_its_own_hash(hasher: Argon2PasswordHasher) -> None:
    """The ordinary path."""
    assert hasher.verify(_secret(PASSWORD), hasher.hash(_secret(PASSWORD))) is True


def test_a_wrong_password_does_not_verify(hasher: Argon2PasswordHasher) -> None:
    """Including one differing only in case, which is the near miss."""
    assert hasher.verify(_secret(WRONG), hasher.hash(_secret(PASSWORD))) is False


def test_a_wrong_password_is_an_answer_rather_than_an_exception(
    hasher: Argon2PasswordHasher,
) -> None:
    """At this layer a wrong password is ordinary.

    Turning it into an authentication failure -- and making that failure
    indistinguishable from an unknown subject -- is ADR-072's requirement and
    belongs to the use case, which is also what writes the audit record.
    """
    stored = hasher.hash(_secret(PASSWORD))

    assert hasher.verify(_secret(WRONG), stored) is False  # no raise


def test_the_same_password_hashes_differently_every_time(
    hasher: Argon2PasswordHasher,
) -> None:
    """Per-call salting, and why the salt is not a parameter.

    A caller that can supply a salt can supply the same salt twice, at which
    point two operators who chose the same password become visibly identical in
    the table -- which tells an attacker which accounts to try first.
    """
    first = hasher.hash(_secret(PASSWORD))
    second = hasher.hash(_secret(PASSWORD))

    assert first != second
    assert hasher.verify(_secret(PASSWORD), first)
    assert hasher.verify(_secret(PASSWORD), second)


def test_the_hash_is_not_the_password(hasher: Argon2PasswordHasher) -> None:
    """One-way, asserted rather than assumed."""
    encoded = hasher.hash(_secret(PASSWORD)).encoded

    assert PASSWORD not in encoded


def test_the_encoded_form_is_self_describing(hasher: Argon2PasswordHasher) -> None:
    """Argon2 carries its algorithm, parameters and salt inline.

    Which is why verification needs nothing but the hash and the password, and
    why raising the work factor later does not invalidate existing hashes.
    """
    assert hasher.hash(_secret(PASSWORD)).encoded.startswith("$argon2id$")


# --------------------------------------------------------------------------- #
# Failure modes collapse to one answer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "unreadable",
    ["not-a-hash", "$argon2id$broken", "$unknown$v=19$m=1,t=1,p=1$c2FsdA$aGFzaA"],
)
def test_an_unreadable_stored_hash_verifies_as_false(
    hasher: Argon2PasswordHasher, unreadable: str
) -> None:
    """Not an exception, and deliberately not distinguishable from a wrong password.

    From the caller's position both mean "this password does not authenticate
    this principal". Distinguishing them would leak the state of the table --
    and a corrupt hash is still visible where it should be, as a run of failed
    authentications for one subject in the audit log.
    """
    assert hasher.verify(_secret(PASSWORD), PasswordHash(unreadable)) is False


def test_verify_nothing_never_raises_and_never_succeeds(
    hasher: Argon2PasswordHasher,
) -> None:
    """The timing-equalisation call, on the path where no principal matched.

    ADR-072 requires "no such user" and "wrong password" to be
    indistinguishable. The same error is half of it; the other half is the
    stopwatch, because a lookup that misses returns in microseconds while a real
    verification spends deliberate milliseconds. This is the call that pays the
    difference, and it must be impossible for it to authenticate anybody.
    """
    # Nothing is asserted about a return value because there is none: the method
    # returns ``None`` by design, so that no caller can mistake it for a
    # verification that succeeded. The assertion is that neither call raises --
    # pytest fails this test if either does -- and that the empty password is
    # handled on the same path as a real one.
    hasher.verify_nothing(_secret(PASSWORD))
    hasher.verify_nothing(_secret(""))


def test_nothing_verifies_against_the_dummy_hash(hasher: Argon2PasswordHasher) -> None:
    """The equalisation hash must not be reachable as a credential.

    It is a module constant, so the risk is that somebody could authenticate by
    submitting it. They cannot: ``verify_nothing`` discards its result and
    returns ``None`` regardless, so there is no value a caller could branch on.

    The positive half is asserted through :meth:`verify` instead, which is the
    method that *can* return true -- and does not, for the dummy.
    """
    dummy = _secret("\x00 no principal matched \x00")

    hasher.verify_nothing(dummy)

    assert hasher.verify(dummy, hasher.hash(_secret(PASSWORD))) is False


# --------------------------------------------------------------------------- #
# The deliberate leak test (ADR-037)
# --------------------------------------------------------------------------- #


def test_a_hash_reaching_a_log_line_is_redacted(hasher: Argon2PasswordHasher) -> None:
    """The G0 checklist's leak test, applied to S06's own type.

    Argon2 parameters are not a password, but they tell an attacker exactly how
    much work a crack costs. ``PasswordHash`` redacts on every route that turns
    an object into text, so the ordinary way a value reaches a log line shows a
    marker instead.
    """
    stored = hasher.hash(_secret(PASSWORD))

    rendered = f"authentication failed for principal with hash {stored}"

    assert stored.encoded not in rendered
    assert PASSWORD not in rendered


def test_the_password_itself_is_a_secret_value_throughout(
    hasher: Argon2PasswordHasher,
) -> None:
    """The plaintext never appears as a bare ``str`` in this adapter's signature.

    ``SecretValue`` is registered for redaction (ADR-037), so a password that
    reaches a log line by any route is masked. Accepting a bare string here
    would put the one genuinely reversible value in this subsystem outside that
    protection.
    """
    password = _secret(PASSWORD)

    assert PASSWORD not in str(password)
    assert hasher.verify(password, hasher.hash(password)) is True
