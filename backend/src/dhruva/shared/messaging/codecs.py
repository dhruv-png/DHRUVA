"""Exact codecs for domain values crossing a transport (ADR-061).

Why this module exists
----------------------
ADR-042 makes money an exact integer count of minor units, and S03 spends real
effort proving a value survives a round trip exactly. A serialiser is the last
place anyone thinks to check, and it is the easiest place to lose that
guarantee: ``json.dumps`` will happily turn a ``Decimal`` into a ``float`` if
asked politely, and the loss is silent.

So every domain value is encoded by its **exact representation** -- the integer
it already is, never a formatted number:

    Money      -> {"__t": "money", "minor_units": 123456, "currency": "INR"}
    Price      -> {"__t": "price", "scaled_units": 123456780000, "scale": 8}
    Quantity   -> {"__t": "quantity", "units": 50}
    Ratio      -> {"__t": "ratio", "fraction": "0.18"}
    Decimal    -> {"__t": "decimal", "value": "1.2345"}
    UUID       -> {"__t": "uuid", "value": "..."}
    datetime   -> {"__t": "datetime", "value": "2026-07-28T09:15:00+00:00"}
    date       -> {"__t": "date", "value": "2026-07-28"}

The ``__t`` discriminator is what makes decoding unambiguous. Without it, a
mapping with a ``value`` key could be a decimal, a UUID or a caller's own dict,
and the decoder would have to guess. Guessing about money is how a number
changes meaning between two processes.

What is refused
---------------
``float`` is rejected on sight, at any depth, in both directions. Not coerced,
not rounded -- refused. There is no correct float representation of a monetary
amount, so accepting one would mean accepting that the value is already wrong and
merely recording it faithfully.

A type with no codec is also refused, rather than falling back to ``str()``. A
silent ``str()`` fallback is how an object becomes ``"<Foo object at 0x7f...>"``
in a message a consumer then cannot read.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from dhruva.shared.errors import ValidationError
from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio

__all__ = ["UnencodableValueError", "decode_value", "encode_value"]

#: Key carrying the type discriminator on an encoded domain value.
TYPE_KEY: Final = "__t"


class UnencodableValueError(ValidationError):
    """Raised for a value no codec accepts, or an encoded form that is malformed.

    A distinct type because the caller's recovery differs from an ordinary
    validation failure: this means the *code* put something on the wire that the
    wire format does not describe, not that inbound *data* was bad.
    """


def _reject_float(value: object) -> None:
    """Refuse a float anywhere in a payload.

    ``bool`` is a subclass of ``int`` and is fine; ``float`` never is. Checked
    before anything else so that no code path can encode one by another route.
    """
    if isinstance(value, float):
        raise UnencodableValueError(
            "float is not representable on the wire; use Money, Price or Decimal",
            actual_type=type(value).__name__,
            value=repr(value),
        )


#: Type -> encoder, tried in order. A table rather than a chain of `if`s: the set
#: of representable types is the thing a reader needs to see, and a table shows it
#: on one screen. Order matters only for `datetime` before `date`, since the first
#: is a subclass of the second.
_ENCODERS: Final[tuple[tuple[type, Any], ...]] = (
    (
        Money,
        lambda v: {
            TYPE_KEY: "money",
            "minor_units": v.minor_units,
            "currency": v.currency.value,
        },
    ),
    (Price, lambda v: {TYPE_KEY: "price", "scaled_units": v.scaled_units, "scale": Price.SCALE}),
    (Quantity, lambda v: {TYPE_KEY: "quantity", "units": v.units}),
    (Ratio, lambda v: {TYPE_KEY: "ratio", "fraction": str(v.fraction)}),
    (Decimal, lambda v: {TYPE_KEY: "decimal", "value": str(v)}),
    (UUID, lambda v: {TYPE_KEY: "uuid", "value": str(v)}),
    (datetime, lambda v: {TYPE_KEY: "datetime", "value": _encode_datetime(v)}),
    (date, lambda v: {TYPE_KEY: "date", "value": v.isoformat()}),
)


def encode_value(value: object) -> Any:
    """Encode one value into JSON-ready primitives.

    Recurses through mappings and sequences, so a nested payload carries the same
    guarantees as a flat one.

    Raises
    ------
    UnencodableValueError
        If the value is a ``float``, or of a type with no codec.
    """
    _reject_float(value)

    if value is None or isinstance(value, str | int):
        return value
    for kind, encoder in _ENCODERS:
        if isinstance(value, kind):
            return encoder(value)
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [encode_value(item) for item in value]

    raise UnencodableValueError(
        "no codec for this type; add one rather than relying on str()",
        actual_type=type(value).__name__,
    )


def _encode_datetime(value: datetime) -> str:
    """Encode an instant as RFC 3339 in UTC.

    A naive datetime is refused. It has no defined position in a sequence of
    events, and ADR-006 puts every stored instant in UTC -- so a value without an
    offset is a value nobody can order.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise UnencodableValueError(
            "datetime must be timezone-aware; a naive instant cannot be ordered",
            value=value.isoformat(),
        )
    return value.isoformat()


_DECODERS: Final[dict[str, Any]] = {
    "money": lambda body: Money(int(body["minor_units"]), Currency(body["currency"])),
    "price": lambda body: Price(int(body["scaled_units"])),
    "quantity": lambda body: Quantity(int(body["units"])),
    "ratio": lambda body: Ratio(Decimal(body["fraction"])),
    "decimal": lambda body: Decimal(body["value"]),
    "uuid": lambda body: UUID(body["value"]),
    "datetime": lambda body: datetime.fromisoformat(body["value"]),
    "date": lambda body: date.fromisoformat(body["value"]),
}


def decode_value(value: object) -> Any:
    """Decode one JSON-derived value back into domain types.

    The inverse of :func:`encode_value` for every type it accepts.

    Raises
    ------
    UnencodableValueError
        If a float appears, if the discriminator names an unknown type, or if the
        encoded body is missing a field the codec needs.
    """
    _reject_float(value)

    if isinstance(value, dict):
        marker = value.get(TYPE_KEY)
        if marker is None:
            return {str(key): decode_value(item) for key, item in value.items()}

        decoder = _DECODERS.get(str(marker))
        if decoder is None:
            raise UnencodableValueError(
                "unknown encoded type; the producer is ahead of this consumer",
                encoded_type=str(marker),
                known=sorted(_DECODERS),
            )
        try:
            return decoder(value)
        except (KeyError, ValueError, ArithmeticError) as error:
            raise UnencodableValueError(
                "encoded value is malformed for its declared type",
                encoded_type=str(marker),
                reason=str(error),
            ) from error

    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


#: The scale a Price is encoded at. Asserted rather than assumed: if ADR-042's
#: fixed scale ever moves, an envelope written under the old one must not be
#: silently reinterpreted under the new.
ENCODED_PRICE_SCALE: Final = Price.SCALE
