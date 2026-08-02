"""The principal and refresh-token mappings, round-tripped without a database.

Domain <-> record <-> model kwargs. The database round trip is an integration
concern (ADR-058); what is verified here is that nothing is lost or invented at a
layer boundary.

Two properties carry more weight than losslessness in general. The password hash
must come back as a :class:`PasswordHash` and not as a string, because that type
is what makes a plaintext in the field unrepresentable. And a refresh token's
``lineage_id`` must survive exactly, because a token whose lineage drifted would
be revoked by nothing and would still authenticate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.contexts.platform.domain.identity import (
    EncryptedSecret,
    PasswordHash,
    Principal,
    RefreshToken,
)
from dhruva.contexts.platform.infrastructure.persistence.factories import (
    PrincipalFactory,
    RefreshTokenFactory,
)
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_principal_model_kwargs,
    to_principal_record,
    to_refresh_token_model_kwargs,
    to_refresh_token_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import (
    PrincipalModel,
    RefreshTokenModel,
)
from dhruva.contexts.platform.infrastructure.persistence.records import (
    PrincipalRecord,
    RefreshTokenRecord,
)
from dhruva.shared.errors import DataQualityError
from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId

pytestmark = pytest.mark.unit

PRINCIPALS: Final = PrincipalFactory()
TOKENS: Final = RefreshTokenFactory()

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
PRINCIPAL: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
CREATED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
ENCODED: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
SEALED: Final = EncryptedSecret(ciphertext=b"sealed-totp", wrapped_data_key=b"wrapped-key")


def _principal(**overrides: object) -> Principal:
    defaults: dict[str, object] = {
        "principal_id": PRINCIPAL,
        "account_id": ACCOUNT,
        "subject": "operator@dhruva.local",
        "password_hash": PasswordHash(ENCODED),
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Principal(**{**defaults, **overrides})  # type: ignore[arg-type]


ROOT_TOKEN: Final = RefreshTokenId(UUID("33333333-3333-3333-3333-333333333333"))


def _token(*, token_id: RefreshTokenId | None = None, **overrides: object) -> RefreshToken:
    identity = ROOT_TOKEN if token_id is None else token_id
    defaults: dict[str, object] = {
        "token_id": identity,
        "account_id": ACCOUNT,
        "principal_id": PRINCIPAL,
        "lineage_id": identity.value,
        "token_hash": b"a-digest",
        "issued_at": CREATED,
        "expires_at": CREATED + timedelta(days=30),
    }
    return RefreshToken(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Principal round trips
# --------------------------------------------------------------------------- #


@given(
    subject=st.text(max_size=200).map(lambda tail: f"a{tail}"),
    enrolled=st.booleans(),
    disabled=st.booleans(),
    version=st.integers(min_value=1, max_value=10**6),
)
def test_a_principal_survives_the_round_trip(
    subject: str, *, enrolled: bool, disabled: bool, version: int
) -> None:
    """Every field, in both directions, enrolled or not and disabled or not."""
    original = _principal(
        subject=subject,
        totp_secret=SEALED if enrolled else None,
        disabled_at=CREATED + timedelta(days=1) if disabled else None,
        updated_at=CREATED + timedelta(days=1) if disabled else CREATED,
        version=version,
    )

    assert PRINCIPALS.reconstruct(PRINCIPALS.deconstruct(original)) == original


def test_the_password_hash_comes_back_as_a_type_not_a_string() -> None:
    """The property that makes a plaintext in this field unrepresentable.

    The record carries a primitive so the mapper can stay free of domain types
    (ADR-052). If the factory forgot to lift it back, every layer above would
    quietly accept a bare string -- and the first person to pass a password
    instead of a hash would be storing plaintext.
    """
    reconstructed = PRINCIPALS.reconstruct(PRINCIPALS.deconstruct(_principal()))

    assert isinstance(reconstructed.password_hash, PasswordHash)
    assert reconstructed.password_hash.encoded == ENCODED


def test_enrolment_survives_as_a_pair_or_as_neither() -> None:
    """The two TOTP columns are only meaningful together."""
    flattened = PRINCIPALS.deconstruct(_principal(totp_secret=SEALED))

    assert flattened.totp_secret == SEALED.ciphertext
    assert flattened.totp_wrapped_key == SEALED.wrapped_data_key

    unenrolled = PRINCIPALS.deconstruct(_principal())

    assert unenrolled.totp_secret is None
    assert unenrolled.totp_wrapped_key is None


@pytest.mark.parametrize("present", ["totp_secret", "totp_wrapped_key"])
def test_half_a_sealed_secret_fails_on_read(present: str) -> None:
    """A ciphertext with no wrapped key is a secret nothing can open.

    The table refuses this pair with a check constraint, so reaching here means
    the constraint was dropped. Treating it defensively as "not enrolled" would
    be worse than failing: ADR-073 gates the order permission on enrolment, so
    silently reporting an enrolled principal as unenrolled changes an
    authorisation answer.
    """
    flattened = PRINCIPALS.deconstruct(_principal(totp_secret=SEALED))
    half = PrincipalRecord(
        **{
            **{name: getattr(flattened, name) for name in flattened.__dataclass_fields__},
            "totp_secret": SEALED.ciphertext if present == "totp_secret" else None,
            "totp_wrapped_key": SEALED.wrapped_data_key if present == "totp_wrapped_key" else None,
        }
    )

    with pytest.raises(DataQualityError):
        PRINCIPALS.reconstruct(half)


def test_the_principal_model_and_record_describe_the_same_row() -> None:
    """Catches a column added to one and not the other. Either way is silent."""
    columns = {column.name for column in PrincipalModel.__table__.columns}

    assert columns == {f.name for f in PrincipalRecord.__dataclass_fields__.values()}


def test_principal_kwargs_cover_every_column() -> None:
    """A field added to the record but forgotten in the mapper would be silent."""
    flattened = PRINCIPALS.deconstruct(_principal())

    assert set(to_principal_model_kwargs(flattened)) == set(flattened.__dataclass_fields__)


def test_a_principal_model_maps_back_field_for_field() -> None:
    """The read direction, which the round trip above does not exercise."""
    flattened = PRINCIPALS.deconstruct(_principal(totp_secret=SEALED))
    model = PrincipalModel(**to_principal_model_kwargs(flattened))

    assert to_principal_record(model) == flattened


# --------------------------------------------------------------------------- #
# Refresh token round trips
# --------------------------------------------------------------------------- #


@given(
    digest=st.binary(min_size=1, max_size=128),
    revoked=st.booleans(),
    replaced=st.booleans(),
)
def test_a_refresh_token_survives_the_round_trip(
    digest: bytes, *, revoked: bool, replaced: bool
) -> None:
    """Every field, including the optional links that carry the chain."""
    original = _token(
        token_hash=digest,
        revoked_at=CREATED + timedelta(hours=1) if revoked else None,
        replaced_by=RefreshTokenId.new() if replaced else None,
    )

    assert TOKENS.reconstruct(TOKENS.deconstruct(original)) == original


def test_the_lineage_survives_exactly() -> None:
    """The field revocation matches on.

    A token whose lineage came back changed would be revoked by nothing and
    would still authenticate -- a live session inside a family somebody believed
    they had killed.
    """
    root = _token()
    _, successor = root.succeeded_by(
        token_id=RefreshTokenId.new(),
        token_hash=b"next",
        issued_at=CREATED + timedelta(minutes=5),
        expires_at=CREATED + timedelta(days=30),
    )

    assert TOKENS.reconstruct(TOKENS.deconstruct(successor)).lineage_id == root.lineage_id


def test_a_successor_keeps_its_parent_link_through_the_mapping() -> None:
    """``parent_token_id`` is what a forensic reader walks after the fact."""
    _, successor = _token().succeeded_by(
        token_id=RefreshTokenId.new(),
        token_hash=b"next",
        issued_at=CREATED + timedelta(minutes=5),
        expires_at=CREATED + timedelta(days=30),
    )

    reconstructed = TOKENS.reconstruct(TOKENS.deconstruct(successor))

    assert reconstructed.parent_token_id == successor.parent_token_id


def test_the_refresh_token_model_and_record_describe_the_same_row() -> None:
    """Catches a column added to one and not the other."""
    columns = {column.name for column in RefreshTokenModel.__table__.columns}

    assert columns == {f.name for f in RefreshTokenRecord.__dataclass_fields__.values()}


def test_refresh_token_kwargs_cover_every_column() -> None:
    """A field added to the record but forgotten in the mapper would be silent."""
    flattened = TOKENS.deconstruct(_token())

    assert set(to_refresh_token_model_kwargs(flattened)) == set(flattened.__dataclass_fields__)


def test_a_refresh_token_model_maps_back_field_for_field() -> None:
    """The read direction."""
    flattened = TOKENS.deconstruct(_token())
    model = RefreshTokenModel(**to_refresh_token_model_kwargs(flattened))

    assert to_refresh_token_record(model) == flattened


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_the_records_carry_no_domain_types() -> None:
    """A record is the layer with no invariants, so a mapper needs no domain."""
    permitted = {
        "UUID",
        "UUID | None",
        "str",
        "bytes",
        "bytes | None",
        "int",
        "datetime",
        "datetime | None",
    }

    for record in (PrincipalRecord, RefreshTokenRecord):
        for name, annotation in record.__annotations__.items():
            assert str(annotation) in permitted, f"{record.__name__}.{name} is a domain type"


def test_the_refresh_token_record_has_no_version_field() -> None:
    """Rotation's ``WHERE replaced_by IS NULL`` is already the concurrency control.

    A version column would be a second mechanism for a race the first resolves
    correctly, and it would make one winner and one loser look like a lost
    update instead.
    """
    assert "version" not in RefreshTokenRecord.__dataclass_fields__
