"""The credential aggregate and its record binding (ADR-070, ADR-057, TD-S06-6).

Two things are under test. The aggregate's invariants, which decide what may be
stored at all; and :func:`credential_associated_data`, which decides what a
stored ciphertext may later be opened from. The second is the more valuable: the
binding is only worth having if two different records can never produce the same
one, so most of what follows is about collisions.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

import dhruva.contexts.platform.domain.identity.credentials as credentials_module
from dhruva.contexts.platform.domain.identity.credentials import (
    BROKER_MAX_LENGTH,
    CREDENTIAL_PURPOSE_MAX_LENGTH,
    Credential,
    CredentialPurpose,
    EncryptedSecret,
    credential_associated_data,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, CredentialId

pytestmark = pytest.mark.unit

CREATED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)
UPDATED: Final = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)
CREDENTIAL: Final = CredentialId.deterministic("credential", "one")
ACCOUNT: Final = AccountId.deterministic("primary")
BROKER: Final = "zerodha"
PURPOSE: Final = CredentialPurpose.ENROLMENT
SEALED: Final = EncryptedSecret(ciphertext=b"sealed-bytes", wrapped_data_key=b"wrapped-bytes")

#: Broker names ``_valid_broker`` accepts, for the collision properties below.
_broker_names = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-_"),
    min_size=1,
    max_size=BROKER_MAX_LENGTH,
)


def _credential(**overrides: object) -> Credential:
    defaults: dict[str, object] = {
        "credential_id": CREDENTIAL,
        "account_id": ACCOUNT,
        "broker": BROKER,
        "purpose": PURPOSE,
        "secret": SEALED,
        "created_at": CREATED,
        "updated_at": UPDATED,
    }
    return Credential(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The binding
# --------------------------------------------------------------------------- #


def test_the_binding_is_deterministic() -> None:
    """It must be: a credential written by one process is read by another."""
    first = credential_associated_data(CREDENTIAL, ACCOUNT, BROKER, PURPOSE)
    second = credential_associated_data(CREDENTIAL, ACCOUNT, BROKER, PURPOSE)

    assert first == second


def test_the_binding_carries_a_scheme_version() -> None:
    """So that changing the encoding fails loudly rather than silently.

    Without a tag, a future change to the field order or separator would produce
    a binding that is merely *different* -- every existing ciphertext would stop
    opening with no indication of why. The tag makes the cause readable in a hex
    dump.
    """
    assert credential_associated_data(CREDENTIAL, ACCOUNT, BROKER, PURPOSE).startswith(
        b"dhruva.credential.aad.v2"
    )


def test_the_scheme_version_moved_when_purpose_joined_the_binding() -> None:
    """ADR-077 bumped v1 to v2, and the bump is the security-relevant part.

    A widened v1 would leave ciphertext sealed before the distinction existed
    openable under a construction that no longer tells an owner's long-lived
    secret apart from a day's session token. Failing closed costs nothing today
    -- no rows exist -- and is the behaviour that would have been wanted if any
    had.
    """
    binding = credential_associated_data(CREDENTIAL, ACCOUNT, BROKER, PURPOSE)

    assert b"dhruva.credential.aad.v1" not in binding


@pytest.mark.parametrize(
    ("credential_id", "account_id", "broker", "purpose"),
    [
        (CredentialId.deterministic("credential", "two"), ACCOUNT, BROKER, PURPOSE),
        (CREDENTIAL, AccountId.deterministic("secondary"), BROKER, PURPOSE),
        (CREDENTIAL, ACCOUNT, "another", PURPOSE),
        (CREDENTIAL, ACCOUNT, BROKER, CredentialPurpose.SESSION),
    ],
    ids=["credential-id", "account-id", "broker", "purpose"],
)
def test_changing_any_bound_fact_changes_the_binding(
    credential_id: CredentialId,
    account_id: AccountId,
    broker: str,
    purpose: CredentialPurpose,
) -> None:
    """All four participate. A fact that did not would not be bound.

    The purpose case is the one ADR-077 added, and the one that makes the
    separation cryptographic rather than merely conventional.
    """
    assert credential_associated_data(
        credential_id, account_id, broker, purpose
    ) != credential_associated_data(CREDENTIAL, ACCOUNT, BROKER, PURPOSE)


def test_the_identifiers_contribute_their_typed_form_not_a_bare_uuid() -> None:
    """So a credential and an account sharing a UUID still bind distinguishably.

    With bare UUIDs the two fields would be interchangeable in that case, and a
    credential re-parented to the account whose identifier it happened to match
    would open exactly as before.
    """
    shared = CREDENTIAL.value
    binding = credential_associated_data(CredentialId(shared), AccountId(shared), BROKER, PURPOSE)

    assert b"cred_" in binding
    assert b"acct_" in binding
    assert binding.count(str(shared).encode()) == 2


@given(
    first_broker=_broker_names,
    second_broker=_broker_names,
    first_suffix=st.text(min_size=1, max_size=8),
    second_suffix=st.text(min_size=1, max_size=8),
    first_purpose=st.sampled_from(CredentialPurpose),
    second_purpose=st.sampled_from(CredentialPurpose),
)
def test_two_different_records_never_share_a_binding(  # noqa: PLR0913 - one argument per bound fact
    *,
    first_broker: str,
    second_broker: str,
    first_suffix: str,
    second_suffix: str,
    first_purpose: CredentialPurpose,
    second_purpose: CredentialPurpose,
) -> None:
    """The property the whole construction rests on.

    If two records could produce the same associated data, one's ciphertext would
    open in the other -- which is exactly the attack TD-S06-6 names, reintroduced
    through an ambiguous encoding rather than through a missing one.
    """
    first_id = CredentialId.deterministic("credential", first_suffix)
    second_id = CredentialId.deterministic("credential", second_suffix)
    first = credential_associated_data(first_id, ACCOUNT, first_broker, first_purpose)
    second = credential_associated_data(second_id, ACCOUNT, second_broker, second_purpose)

    if (first_id, first_broker, first_purpose) == (second_id, second_broker, second_purpose):
        assert first == second
    else:
        assert first != second


@pytest.mark.parametrize(
    "broker",
    ["", "Zerodha", "zero dha", "zerodha\x1f", "zerodhá", "zerodha!"],
    ids=["empty", "uppercase", "space", "separator", "non-ascii", "punctuation"],
)
def test_an_unsafe_broker_name_cannot_be_bound(broker: str) -> None:
    """Restrictive on purpose: two spellings of a broker are two bindings.

    The separator case is the load-bearing one. A broker name containing the
    field separator could impersonate the boundary between two fields, which is
    how an unambiguous encoding stops being one.
    """
    with pytest.raises(InvariantViolation):
        credential_associated_data(CREDENTIAL, ACCOUNT, broker, PURPOSE)


# --------------------------------------------------------------------------- #
# The aggregate
# --------------------------------------------------------------------------- #


def test_a_credential_exposes_its_own_binding() -> None:
    """Derived, never stored, so it cannot drift from the row it describes."""
    assert _credential().associated_data == credential_associated_data(
        CREDENTIAL, ACCOUNT, BROKER, PURPOSE
    )


def test_a_credential_is_frozen() -> None:
    """A credential edited in place is one whose ciphertext no longer matches it."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        _credential().broker = "someone-else"  # type: ignore[misc]


def test_a_credential_has_no_accessor_returning_a_plaintext() -> None:
    """ADR-070, asserted on the shape of the aggregate.

    There is no method here that could return a secret, because there is no key
    material to open one with. Obtaining a plaintext is a separate call taking a
    ``KeyProvider``, and this is what makes that structurally true rather than
    merely conventional.
    """
    public = {name for name in dir(Credential) if not name.startswith("_")}

    assert public == {"associated_data", "broker", "purpose", "resealed"} | {
        "account_id",
        "created_at",
        "credential_id",
        "key_version",
        "rotated_at",
        "secret",
        "updated_at",
        "version",
    }
    assert not any("reveal" in name or "plaintext" in name for name in public)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"broker": "Zerodha"}, "broker must be"),
        ({"purpose": "ENROLMENT"}, "supported credential purpose"),
        ({"purpose": None}, "supported credential purpose"),
        ({"broker": "z" * (BROKER_MAX_LENGTH + 1)}, "too long"),
        ({"secret": EncryptedSecret(b"", b"wrapped")}, "sealed material"),
        ({"secret": EncryptedSecret(b"sealed", b"")}, "sealed material"),
        ({"key_version": 0}, "key_version starts at one"),
        ({"version": 0}, "version starts at one"),
        ({"created_at": datetime(2026, 8, 1, 9, 15)}, "timezone-aware"),  # noqa: DTZ001
        ({"updated_at": CREATED - timedelta(seconds=1)}, "cannot precede"),
    ],
    ids=[
        "uppercase-broker",
        "purpose-as-string",
        "purpose-missing",
        "overlong-broker",
        "no-ciphertext",
        "no-wrapped-key",
        "zero-key-version",
        "zero-version",
        "naive-timestamp",
        "updated-before-created",
    ],
)
def test_an_unstorable_credential_is_refused(overrides: dict[str, object], match: str) -> None:
    """Each invariant, and what it is protecting.

    The empty-sealed-material cases matter most. Every value the envelope
    produces carries a nonce and a tag even when the plaintext was empty, so an
    empty blob cannot have come from it -- refusing one means a row that was
    never sealed cannot pass as a row that was.
    """
    with pytest.raises(InvariantViolation, match=match):
        _credential(**overrides)


def test_a_naive_rotation_timestamp_is_refused() -> None:
    """ADR-006 applies to the optional field too, not only the required ones."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        _credential(rotated_at=datetime(2026, 8, 1, 9, 20))  # noqa: DTZ001


# --------------------------------------------------------------------------- #
# Purpose
# --------------------------------------------------------------------------- #


def test_the_purposes_are_exactly_the_two_this_decision_named() -> None:
    """Closed, and closed at two (ADR-077).

    A third member would need a decision, because the value of the distinction
    is that every stored secret falls on one side of it. Asserting the whole set
    means a member added without an ADR fails here rather than quietly acquiring
    a check constraint the migration never allowed.
    """
    assert {member.value for member in CredentialPurpose} == {"ENROLMENT", "SESSION"}


def test_a_purpose_cannot_be_an_arbitrary_string() -> None:
    """The enum is the whole guard against a third meaning appearing silently."""
    with pytest.raises(ValueError, match="ENROLMENT_AND_SESSION"):
        CredentialPurpose("ENROLMENT_AND_SESSION")


def test_the_purposes_name_no_vendor() -> None:
    """Generic on purpose: these are properties of secrets, not of Zerodha.

    ``ENROLMENT`` and ``SESSION`` describe lifecycles any broker with an
    application credential and a login has. A member named after one provider
    would put a vendor into the shared identity domain, which is exactly what
    ADR-003 keeps out of it.
    """
    vendors = ("zerodha", "kite", "upstox", "angel", "dhan")

    for member in CredentialPurpose:
        assert not any(vendor in member.value.lower() for vendor in vendors)


def test_every_purpose_fits_the_column_it_is_stored_in() -> None:
    """The enum and the ``VARCHAR(16)`` must not be able to drift apart.

    A longer member would be refused by the database rather than by the domain,
    which turns a design mistake into a runtime write failure on the one table
    whose writes carry secrets.
    """
    for member in CredentialPurpose:
        assert len(member.value) <= CREDENTIAL_PURPOSE_MAX_LENGTH


def test_two_purposes_for_one_broker_are_distinct_credentials() -> None:
    """The point of the whole change, at the aggregate level."""
    enrolment = _credential(purpose=CredentialPurpose.ENROLMENT)
    session = _credential(purpose=CredentialPurpose.SESSION)

    assert enrolment.associated_data != session.associated_data


def test_resealing_cannot_change_a_credential_purpose() -> None:
    """A rotation replaces a secret within a lifecycle; it does not move it.

    If a reseal could re-purpose a row, a daily login could quietly overwrite the
    owner's enrolment material -- the failure ADR-077 exists to make impossible.
    """
    original = _credential(purpose=CredentialPurpose.ENROLMENT)

    rotated = original.resealed(EncryptedSecret(b"new", b"new"), at=UPDATED)

    assert rotated.purpose is CredentialPurpose.ENROLMENT
    assert "purpose" not in inspect.signature(Credential.resealed).parameters


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def test_resealing_advances_the_version_and_leaves_the_original_alone() -> None:
    """ADR-057: a rotation is a new version, not an edit."""
    original = _credential()
    replacement = EncryptedSecret(b"new-sealed", b"new-wrapped")
    at = UPDATED + timedelta(days=30)

    rotated = original.resealed(replacement, at=at)

    assert rotated.version == original.version + 1
    assert rotated.secret == replacement
    assert rotated.updated_at == at
    assert rotated.rotated_at == at
    assert original.secret == SEALED, "the original must be unchanged"
    assert original.rotated_at is None


def test_resealing_keeps_the_key_version_unless_the_master_key_changed() -> None:
    """Re-sealing under the same master key is not a key rotation.

    S42's rotation job finds work by reading ``key_version``. Bumping it for an
    ordinary secret change would tell that job a row had moved to a master key it
    never wrapped it with.
    """
    original = _credential(key_version=3)

    rotated = original.resealed(EncryptedSecret(b"new", b"new"), at=UPDATED)

    assert rotated.key_version == 3
    assert rotated.resealed(EncryptedSecret(b"x", b"y"), at=UPDATED, key_version=4).key_version == 4


def test_a_rotation_cannot_predate_the_last_write() -> None:
    """A vault whose timestamps go backwards is one whose ordering means nothing."""
    with pytest.raises(InvariantViolation, match="cannot predate"):
        _credential().resealed(SEALED, at=CREATED)


def test_a_rotation_time_must_be_timezone_aware() -> None:
    """ADR-011 and ADR-006 together: injected time, and never naive."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        _credential().resealed(SEALED, at=datetime(2026, 9, 1, 9, 30))  # noqa: DTZ001


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_the_credential_module_imports_no_cipher_and_no_persistence_framework() -> None:
    """Rule R7, and the cipher separation rule R10 would formalise.

    Parsed rather than grepped, so a name in a docstring does not fail the build
    and an aliased import cannot pass it. Until rule R10 exists this test is the
    enforcement for this module, exactly as ``test_ports`` is for the ports.
    """
    tree = ast.parse(pathlib.Path(credentials_module.__file__ or "").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imported.isdisjoint(
        {"cryptography", "hashlib", "hmac", "secrets", "sqlalchemy", "alembic", "asyncpg"}
    )
