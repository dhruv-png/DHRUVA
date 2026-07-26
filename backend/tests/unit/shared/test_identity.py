"""Surrogate identifiers: provider-independent, stable, and type-distinct."""

from __future__ import annotations

import ast
import pathlib
import pickle
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

import dhruva.shared.identity as identity_module
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit


def test_a_fresh_identifier_is_unique() -> None:
    """Minted, not derived. Two calls never collide."""
    assert InstrumentId.new() != InstrumentId.new()


def test_a_deterministic_identifier_is_stable_across_calls() -> None:
    """Re-importing reference data must not mint duplicates.

    The natural key uses values the *exchange* defines -- exchange, symbol,
    expiry -- never a broker token, which is not stable across contract cycles.
    """
    first = InstrumentId.deterministic("NSE", "RELIANCE", "EQ")
    second = InstrumentId.deterministic("NSE", "RELIANCE", "EQ")

    assert first == second


def test_different_natural_keys_give_different_identifiers() -> None:
    """Including when the parts are the same strings in a different order."""
    assert InstrumentId.deterministic("NSE", "RELIANCE") != InstrumentId.deterministic(
        "RELIANCE", "NSE"
    )
    assert InstrumentId.deterministic("NSE", "RELIANCE") != InstrumentId.deterministic(
        "BSE", "RELIANCE"
    )


def test_parts_cannot_be_ambiguously_joined() -> None:
    """``("AB", "C")`` and ``("A", "BC")`` must not collide.

    A naive join on a printable separator would make them identical. The
    separator is a control character precisely because it cannot occur in an
    exchange symbol.
    """
    assert InstrumentId.deterministic("AB", "C") != InstrumentId.deterministic("A", "BC")


@pytest.mark.parametrize("parts", [(), ("",), ("NSE", "")])
def test_an_empty_natural_key_is_refused(parts: tuple[str, ...]) -> None:
    """An identifier derived from nothing collides with every other such."""
    with pytest.raises(InvariantViolation):
        InstrumentId.deterministic(*parts)


@given(value=st.uuids())
def test_string_form_round_trips(value: uuid.UUID) -> None:
    """Persisted and logged as a string; must parse back exactly."""
    identifier = InstrumentId(value)

    assert InstrumentId.parse(str(identifier)) == identifier


@given(value=st.uuids())
def test_a_bare_uuid_parses(value: uuid.UUID) -> None:
    """So identifiers read straight from a database column need no ceremony."""
    assert InstrumentId.parse(str(value)) == InstrumentId(value)


def test_the_string_form_is_prefixed_and_greppable() -> None:
    """``inst_...`` in a log line says what kind of thing it identifies."""
    assert str(InstrumentId.new()).startswith("inst_")
    assert str(AccountId.new()).startswith("acct_")


def test_parsing_the_wrong_prefix_is_refused() -> None:
    """Reading an account identifier as an instrument is worth catching loudly."""
    account = AccountId.new()

    with pytest.raises(InvariantViolation, match="prefix"):
        InstrumentId.parse(str(account))


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "inst_nonsense", "inst_"])
def test_malformed_identifiers_are_refused(bad: str) -> None:
    """A malformed identifier must never become a plausible one."""
    with pytest.raises(InvariantViolation):
        InstrumentId.parse(bad)


@pytest.mark.parametrize("bad", ["a-string", 42, None, b"bytes"])
def test_construction_requires_an_actual_uuid(bad: object) -> None:
    """``InstrumentId("NIFTY")`` must not work.

    Accepting a string is exactly how a broker symbol ends up as an identity,
    which is the coupling ADR-009 exists to prevent.
    """
    with pytest.raises(InvariantViolation, match="requires a UUID"):
        InstrumentId(bad)  # type: ignore[arg-type]


def test_identifiers_of_different_kinds_never_compare_equal() -> None:
    """Even when they wrap the same UUID.

    Treating them as equal would make a cross-type dictionary lookup succeed,
    silently returning the wrong entity.
    """
    shared = uuid.uuid4()

    assert InstrumentId(shared) != AccountId(shared)
    assert hash(InstrumentId(shared)) != hash(AccountId(shared))


@given(value=st.uuids())
def test_identifiers_are_hashable_and_immutable(value: uuid.UUID) -> None:
    """Used as dictionary keys throughout; mutation would corrupt every map."""
    identifier = InstrumentId(value)

    assert {identifier: "v"}[InstrumentId(value)] == "v"
    with pytest.raises(AttributeError):
        identifier._value = uuid.uuid4()


@given(value=st.uuids())
def test_identifiers_survive_pickling(value: uuid.UUID) -> None:
    """They cross process boundaries in Celery tasks from S05."""
    identifier = InstrumentId(value)

    assert pickle.loads(pickle.dumps(identifier)) == identifier  # noqa: S301 - our own object


def test_no_broker_identifier_appears_in_the_module() -> None:
    """Provider independence, asserted rather than assumed.

    The shared kernel must not know a broker exists. If Kite ever appears here,
    an instrument's identity has started depending on who we trade through.
    """
    source = pathlib.Path(identity_module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Prose explaining *why* broker identifiers are excluded is welcome; what
    # must not exist is executable code referring to one. Walking the AST for
    # identifiers and non-docstring literals separates the two exactly.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }

    referenced: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.append(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.append(node.attr)
        elif isinstance(node, ast.alias):
            referenced.append(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            referenced.append(node.value)

    haystack = " ".join(referenced).lower()
    for forbidden in ("kite", "zerodha", "instrument_token", "tradingsymbol"):
        assert forbidden not in haystack, f"broker identifier {forbidden!r} reached the kernel"
