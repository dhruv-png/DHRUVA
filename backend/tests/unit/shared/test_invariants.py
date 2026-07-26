"""Invariant guards."""

from __future__ import annotations

import pytest

import dhruva.shared.invariants as invariants_module
from dhruva.shared.errors import InvariantViolation, SafetyError
from dhruva.shared.invariants import invariant, unreachable

pytestmark = pytest.mark.unit


def test_a_holding_invariant_is_silent() -> None:
    """The overwhelmingly common path must cost nothing but a comparison."""
    invariant(True, "always true")


def test_a_violated_invariant_raises_with_context() -> None:
    """Context lands as structured fields, which is what makes the log useful."""
    with pytest.raises(InvariantViolation) as caught:
        invariant(False, "quantity must be positive", quantity=-5, instrument="NIFTY")

    assert caught.value.context == {"quantity": -5, "instrument": "NIFTY"}
    assert str(caught.value.code) == "DHR-SAF-004"


def test_an_invariant_violation_is_a_safety_error() -> None:
    """ADR-022: an object with broken invariants must not inform a trade.

    Making this a SafetyError rather than a validation error means the Risk
    Engine (S25) can treat it as a refusal rather than as an ordinary failure.
    """
    with pytest.raises(SafetyError):
        invariant(False, "broken")


def test_invariants_are_not_assertions() -> None:
    """``python -O`` strips ``assert``.

    An invariant that vanishes under an optimisation flag is not an invariant,
    and the one deployment where somebody sets ``-O`` is the one where it
    mattered. This is a plain function call, so it cannot be optimised away.
    """
    source = invariants_module.invariant.__code__.co_consts

    assert not any(isinstance(c, str) and c.startswith("assert") for c in source)
    with pytest.raises(InvariantViolation):
        invariant(False, "still raises regardless of optimisation level")


def test_unreachable_always_raises() -> None:
    """Marks a branch the domain model says cannot occur."""
    with pytest.raises(InvariantViolation, match="unreachable"):
        unreachable("closed enum gained a member", member="surprise")


@pytest.mark.parametrize("falsy", [False, 0, "", [], {}, None])
def test_any_falsy_condition_violates(falsy: object) -> None:
    """Truthiness, not identity -- callers pass expressions, not booleans."""
    with pytest.raises(InvariantViolation):
        invariant(bool(falsy), "must be truthy")
