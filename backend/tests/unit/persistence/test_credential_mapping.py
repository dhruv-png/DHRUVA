"""The credential's four-layer mapping, round-tripped without a database.

Domain <-> record <-> model kwargs, and the model's columns against the record's
fields. The database round trip is an integration concern (ADR-058); what is
verified here is that nothing is lost or invented at a layer boundary.

One property matters more than losslessness on its own: the **binding** has to
survive. ADR-070's associated data is computed from the credential's identity,
account and broker, so if any of those three came back from a record even
slightly changed, every stored ciphertext would stop opening -- and it would stop
opening in production, on read, long after the mapping defect was introduced.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

import dhruva.contexts.platform.infrastructure.persistence.mappers as mapper_module
from dhruva.contexts.platform.domain.identity.credentials import Credential, EncryptedSecret
from dhruva.contexts.platform.infrastructure.persistence.factories import CredentialFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_credential_model_kwargs,
    to_credential_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import CredentialModel
from dhruva.contexts.platform.infrastructure.persistence.records import CredentialRecord
from dhruva.shared.identity import AccountId, CredentialId

pytestmark = pytest.mark.unit

FACTORY: Final = CredentialFactory()
CREATED: Final = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


def _credential(**overrides: object) -> Credential:
    defaults: dict[str, object] = {
        "credential_id": CredentialId.deterministic("credential", "one"),
        "account_id": AccountId.deterministic("primary"),
        "broker": "zerodha",
        "secret": EncryptedSecret(ciphertext=b"sealed", wrapped_data_key=b"wrapped"),
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Credential(**{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #


@given(
    ciphertext=st.binary(min_size=1, max_size=512),
    wrapped=st.binary(min_size=1, max_size=512),
    key_version=st.integers(min_value=1, max_value=10**6),
    version=st.integers(min_value=1, max_value=10**6),
    rotated=st.booleans(),
)
def test_domain_to_record_to_domain_is_lossless(
    ciphertext: bytes, wrapped: bytes, key_version: int, version: int, rotated: bool
) -> None:
    """Every field survives both directions, including the bytes.

    Binary columns are the field most likely to be quietly mangled -- an encode,
    a memoryview, a driver returning ``bytearray`` -- and a single altered byte
    turns an openable credential into a ``SafetyError`` nobody can explain.
    """
    original = _credential(
        secret=EncryptedSecret(ciphertext=ciphertext, wrapped_data_key=wrapped),
        key_version=key_version,
        version=version,
        rotated_at=CREATED + timedelta(days=1) if rotated else None,
        updated_at=CREATED + timedelta(days=1) if rotated else CREATED,
    )

    assert FACTORY.reconstruct(FACTORY.deconstruct(original)) == original


def test_the_binding_survives_the_round_trip() -> None:
    """The property the whole credential store depends on.

    Losslessness in general is necessary; this is the case that makes it
    load-bearing. A credential whose identity, account or broker came back
    changed would compute a different binding and never open again.
    """
    original = _credential()

    assert FACTORY.reconstruct(FACTORY.deconstruct(original)).associated_data == (
        original.associated_data
    )


def test_record_to_model_kwargs_covers_every_column() -> None:
    """A field added to the record but forgotten in the mapper would be silent."""
    record = FACTORY.deconstruct(_credential())

    kwargs = to_credential_model_kwargs(record)

    assert set(kwargs) == {f.name for f in record.__dataclass_fields__.values()}


def test_the_model_and_the_record_describe_the_same_row() -> None:
    """Catches a column added to the table and not to the record, or the reverse.

    Either omission is silent: the row saves, and the missing value is simply
    never read back.
    """
    columns = {column.name for column in CredentialModel.__table__.columns}

    assert columns == {f.name for f in CredentialRecord.__dataclass_fields__.values()}


def test_a_model_maps_to_a_record_field_for_field() -> None:
    """The read direction, which the round-trip test above does not exercise.

    ``to_credential_record`` takes a loaded model; the factory never sees one. An
    unconstructed model instance is enough here -- no session, no database, which
    is the point of keeping the mapper pure.
    """
    record = FACTORY.deconstruct(_credential(key_version=4, version=7))
    model = CredentialModel(**to_credential_model_kwargs(record))

    assert to_credential_record(model) == record


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_the_record_carries_no_domain_types() -> None:
    """A record holding an ``EncryptedSecret`` would defeat the layering.

    Pairing the ciphertext with its wrapped key is a domain invariant. The record
    is the layer with no invariants, which is what lets a mapper build one
    without importing the domain at all.
    """
    for name, annotation in CredentialRecord.__annotations__.items():
        assert str(annotation) in {"UUID", "str", "bytes", "int", "datetime | None", "datetime"}, (
            f"{name} is a domain type; records carry primitives only"
        )


def test_the_mapper_imports_no_cipher() -> None:
    """The mapping layer moves sealed bytes and cannot inspect them.

    Authentication belongs to the cipher. A mapper that could decrypt would be a
    second place a plaintext can appear, and ADR-070's whole argument is that
    there is exactly one.
    """
    tree = ast.parse(pathlib.Path(mapper_module.__file__ or "").read_text(encoding="utf-8"))
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

    assert imported.isdisjoint({"cryptography", "hashlib", "hmac", "secrets"})


def test_the_factory_mints_no_identifier() -> None:
    """Unlike the worked example's, which takes a ``row_id`` the repository mints.

    A credential's primary key is its domain identity, because the ciphertext is
    bound to it. An identifier invented at write time could never be recomputed
    on read, so the signature deliberately offers nowhere to invent one.
    """
    record = FACTORY.deconstruct(_credential())
    other = dataclasses.replace(record, id=uuid4())

    assert FACTORY.reconstruct(other).associated_data != FACTORY.reconstruct(record).associated_data
