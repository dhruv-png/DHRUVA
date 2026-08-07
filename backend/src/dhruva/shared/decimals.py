"""One canonical text form for an exact decimal.

A price read back from PostgreSQL carries the column's scale: ``NUMERIC(_, 8)``
returns ``Decimal("100.00000000")`` for a close that was written as
``Decimal("100")``. Both are the same number, and ``str()`` renders them
differently, so anything that serialises a decimal by calling ``str`` produces
output that depends on where the value came from rather than on what it is. In a
snapshot whose entire claim is determinism, that is a defect: two exports of one
close could disagree because one value happened to come through the ORM.

The canonical form is therefore the *value*, not the storage scale: the shortest
exact decimal text, with no trailing zeros, no exponent, and no negative zero.
Trailing scale is a property of the column, not information about the price, so
dropping it loses nothing and makes the output independent of the schema.

Nothing here goes near a binary float. ``format(..., "f")`` on a ``Decimal`` is
exact, which is the whole reason it is used instead of ``str`` or ``repr``.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["canonical_decimal", "canonical_decimal_or_none"]


def canonical_decimal(value: Decimal) -> str:
    """Return the canonical text for an exact decimal.

    Examples
    --------
    ``Decimal("100.00000000")`` and ``Decimal("100")`` both render ``"100"``;
    ``Decimal("0.9300")`` renders ``"0.93"``; ``Decimal("-0.00")`` renders
    ``"0"``, because a signed zero is an artefact of arithmetic rather than a
    number anybody wrote down.

    Raises
    ------
    ValueError
        If the value is not finite. A NaN or an infinity has no exact text and
        must not be smuggled into a file that claims to be exact.
    """
    if not value.is_finite():
        msg = f"a non-finite decimal has no canonical form: {value!r}"
        raise ValueError(msg)
    if value == 0:
        return "0"
    # `normalize` strips trailing zeros but may produce an exponent form --
    # Decimal("100.00").normalize() is Decimal("1E+2") -- so the result is
    # rendered with "f", which is exact and never scientific.
    return format(value.normalize(), "f")


def canonical_decimal_or_none(value: Decimal | None) -> str | None:
    """Return the canonical text, or ``None`` for an absent value.

    Absence stays absence. A missing close must not become ``"0"``, which reads
    as a price rather than as a gap.
    """
    return None if value is None else canonical_decimal(value)
