"""What makes a principal valid, and what a password hash refuses to reveal.

Nothing here touches a database or a hashing library. The principal is a value
object, so every rule it carries is assertable without either -- which is the
point of deciding what a principal *is* before deciding how one is stored.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest

from dhruva.contexts.platform.domain.identity import (
    REDACTED_HASH,
    EncryptedSecret,
    PasswordHash,
    Principal,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, PrincipalId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
PRINCIPAL: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
SEALED: Final = EncryptedSecret(ciphertext=b"sealed", wrapped_data_key=b"wrapped")

#: A realistically shaped argon2 encoded form. Not a password and not a real
#: hash -- the salt and digest are the literals "salt" and "hash" base64-encoded
#: -- so there is nothing here worth redacting, which is the point: the tests
#: below assert that it gets redacted anyway.
ENCODED_HASH: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


def make_principal(**overrides: object) -> Principal:
    """Build a valid principal, overriding one field at a time."""
    fields: dict[str, object] = {
        "principal_id": PRINCIPAL,
        "account_id": ACCOUNT,
        "subject": "operator@dhruva.local",
        "password_hash": PasswordHash(ENCODED_HASH),
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    fields.update(overrides)
    return Principal(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The password hash refuses to be printed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("render", [str, repr, "{}".format, lambda h: f"{h}"])
def test_a_password_hash_never_renders_its_value(render: object) -> None:
    """Every route that turns an object into text, closed.

    ``repr`` matters as much as ``str`` here: it is what a debugger and several
    logging paths call implicitly, so a dataclass default would have put argon2
    parameters exactly where nobody thought to look. Leaked parameters tell an
    attacker how much work a crack costs, which is the one thing the encoded
    form usefully discloses.
    """
    encoded = ENCODED_HASH
    rendered = render(PasswordHash(encoded))  # type: ignore[operator]

    assert encoded not in rendered
    assert REDACTED_HASH in rendered


def test_a_password_hash_still_exposes_its_value_deliberately() -> None:
    """Redaction must not make the hash unusable by the thing that verifies it."""
    encoded = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

    assert PasswordHash(encoded).encoded == encoded


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_password_hash_is_refused(blank: str) -> None:
    """A blank hash is a field somebody failed to fill in.

    It is not the hash of a blank password. Allowing it would produce a principal
    that no password authenticates and none can be checked against -- which
    fails only at the login attempt, and looks exactly like a forgotten password.
    """
    with pytest.raises(InvariantViolation):
        PasswordHash(blank)


def test_a_password_hash_is_a_distinct_type_from_a_string() -> None:
    """The whole reason this class exists (ADR-043's argument, applied to secrets).

    A plaintext password in the field meant for its hash is a silent,
    type-checking, test-passing mistake whose consequence is a database of
    plaintext passwords. This asserts the runtime half; mypy asserts the rest.
    """
    assert not isinstance(PasswordHash("x"), str)


# --------------------------------------------------------------------------- #
# Enrolment is derived, never stored
# --------------------------------------------------------------------------- #


def test_a_principal_without_a_sealed_secret_is_not_enrolled() -> None:
    """Enrolment is ``totp_secret is not None`` and nothing else.

    There is no boolean to disagree with the secret. ADR-073 gates the order
    permission on enrolment, so a boolean that drifted would mean granting order
    placement to an account whose second factor does not work.
    """
    assert make_principal().is_two_factor_enrolled is False


def test_enrolling_a_second_factor_makes_the_principal_enrolled() -> None:
    """The only way the property becomes true is the secret arriving."""
    enrolled = make_principal().enrolled(SEALED, at=CREATED + timedelta(minutes=1))

    assert enrolled.is_two_factor_enrolled is True
    assert enrolled.totp_secret == SEALED


def test_a_principal_cannot_open_its_own_second_factor() -> None:
    """It holds sealed bytes and imports no cipher, exactly as ``Credential`` does.

    Verification of a TOTP code is Step 7 and takes a ``KeyProvider``
    explicitly, which is what keeps the plaintext path greppable (ADR-070).
    """
    enrolled = make_principal().enrolled(SEALED, at=CREATED)

    assert not hasattr(enrolled, "totp_code")
    assert not hasattr(enrolled, "reveal")


# --------------------------------------------------------------------------- #
# Every change is a new version
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.with_password(PasswordHash("$argon2id$new"), at=CREATED),
        lambda p: p.enrolled(SEALED, at=CREATED),
        lambda p: p.disabled(at=CREATED),
    ],
    ids=["password", "enrolment", "disable"],
)
def test_every_change_increments_the_version(change: object) -> None:
    """ADR-057: the aggregate carries the version the repository will write.

    A change that left the version alone would make the optimistic-concurrency
    UPDATE match the row it was supposed to guard, which is a lost update that
    reports success.
    """
    original = make_principal()

    assert change(original).version == original.version + 1  # type: ignore[operator]


def test_a_principal_cannot_be_edited_in_place() -> None:
    """Frozen, so a change is a new object and the old one stays comparable."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_principal().subject = "someone-else"  # type: ignore[misc]


def test_disabling_stops_the_principal_being_active() -> None:
    """A disabled principal is refused at authentication and is not deleted.

    Not deleted because the audit trail references it, and evidence pointing at
    nothing is not evidence.
    """
    disabled = make_principal().disabled(at=CREATED)

    assert make_principal().is_active is True
    assert disabled.is_active is False
    assert disabled.disabled_at == CREATED


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("subject", ["", "   ", "\t"])
def test_a_principal_must_have_a_subject(subject: str) -> None:
    """The subject reaches the token claims and the audit record's actor."""
    with pytest.raises(InvariantViolation):
        make_principal(subject=subject)


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_both_timestamps_must_be_timezone_aware(field: str) -> None:
    """ADR-006: nothing in this platform stores a naive instant."""
    with pytest.raises(InvariantViolation):
        make_principal(**{field: datetime(2026, 8, 2, 9, 15)})  # noqa: DTZ001 - the point


def test_a_principal_cannot_be_updated_before_it_was_created() -> None:
    """A clock defect, caught where it is cheap rather than in a stored row."""
    with pytest.raises(InvariantViolation):
        make_principal(updated_at=CREATED - timedelta(seconds=1))


def test_a_version_below_one_is_refused() -> None:
    """Versions start at 1, matching the column's default and its check."""
    with pytest.raises(InvariantViolation):
        make_principal(version=0)


def test_two_principals_with_the_same_values_are_equal() -> None:
    """Value semantics, so a round trip through persistence is one assertion."""
    assert make_principal() == make_principal()
