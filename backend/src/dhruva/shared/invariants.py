"""Invariant enforcement.

A domain invariant is a statement that must be true of a value or an aggregate
at every observable moment. This module is how one is enforced.

Why not ``assert``
------------------
``python -O`` strips assert statements. An invariant that vanishes under an
optimisation flag is not an invariant, and the one deployment where somebody sets
``-O`` is exactly the deployment where it mattered. :func:`invariant` is an
ordinary function call, so it cannot be optimised away.

It also raises a typed :class:`InvariantViolation` rather than
``AssertionError``, which means it participates in the error taxonomy (ADR-038):
it carries a stable code, structured context, and — because it is a
:class:`~dhruva.shared.errors.SafetyError` — it is distinguishable from an
ordinary failure by anything that needs to make that distinction (ADR-022).
"""

from __future__ import annotations

from typing import Any, Never

from dhruva.shared.errors import InvariantViolation

__all__ = ["InvariantViolation", "invariant", "unreachable"]


def invariant(condition: bool, message: str, /, **context: Any) -> None:
    """Assert a domain invariant, raising if it does not hold.

    Parameters
    ----------
    condition
        Must be ``True``. Evaluated by the caller, so the expression appears at
        the call site where a reader can see it.
    message
        Stable description of the rule, phrased as the rule rather than as the
        violation -- "quantity must be positive", not "quantity was negative".
    **context
        Structured fields describing this occurrence. These land in the log
        record as queryable fields (ADR-038).

    Raises
    ------
    InvariantViolation
        If ``condition`` is falsy.

    Examples
    --------
    >>> invariant(1 > 0, "one must exceed zero")
    >>> invariant(False, "quantity must be positive", quantity=-5)
    Traceback (most recent call last):
        ...
    dhruva.shared.invariants.InvariantViolation: [DHR-SAF-004] quantity must be positive
    """
    if not condition:
        raise InvariantViolation(message, **context)


def unreachable(message: str, /, **context: Any) -> Never:
    """Mark a branch the domain model says cannot be reached.

    Returns :data:`typing.Never`, so the type checker treats any code following a
    call as unreachable and will report it. That makes this useful for exhaustive
    matching over a closed enum: adding a member without handling it becomes a
    type error rather than a silent fall-through.

    Raises
    ------
    InvariantViolation
        Always.
    """
    raise InvariantViolation(f"unreachable: {message}", **context)
