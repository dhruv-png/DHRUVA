"""Surrogate identifiers.

Every entity in the platform is identified by a UUID we mint, never by an
identifier a third party controls (ADR-009).

The reason is specific rather than doctrinal. Zerodha's ``instrument_token`` is
not stable across contract cycles; ``tradingsymbol`` changes on corporate
actions and encodes expiry for derivatives. Either would work as a key right up
until it did not, and the failure would be a position attributed to the wrong
instrument.

Broker identifiers are *attributes with validity windows*, held by the Reference
context (S07). They are how we talk to Kite. They are not who anything is.

Provider independence is structural here: nothing in this module imports or
mentions a broker, and no broker-specific identifier may be embedded in a
:class:`SurrogateId`.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final, Self

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.invariants import invariant

__all__ = ["AccountId", "InstrumentId", "SurrogateId"]

#: Namespace for deterministic identifiers. Fixed forever: changing it would
#: change every derived identifier, which is the same as losing them.
_DHRUVA_NAMESPACE: Final = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


class SurrogateId:
    """A UUID-backed identifier for a domain entity.

    Immutable, hashable, and serialisable to a string that round-trips. Subclasses
    are distinct types even though they share a representation, so an
    :class:`InstrumentId` cannot be passed where an :class:`AccountId` is
    expected -- the same dimensional reasoning as ADR-043, applied to identity.
    """

    #: Prefix used in the readable string form. Overridden by each subclass.
    PREFIX: ClassVar[str] = "id"

    __slots__ = ("_value",)

    _value: uuid.UUID

    def __init__(self, value: uuid.UUID, /) -> None:
        """Wrap a UUID.

        Raises
        ------
        InvariantViolation
            If ``value`` is not a :class:`uuid.UUID`. Accepting a string here
            would make ``InstrumentId("NIFTY")`` succeed, which is exactly the
            broker-identifier coupling this class exists to prevent.
        """
        invariant(
            isinstance(value, uuid.UUID),
            f"{type(self).__name__} requires a UUID",
            value=repr(value),
            actual_type=type(value).__name__,
        )
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse mutation; identifiers are value objects."""
        msg = f"{type(self).__name__} is immutable; cannot set {name!r}"
        raise AttributeError(msg)

    @classmethod
    def new(cls) -> Self:
        """Mint a fresh random identifier."""
        return cls(uuid.uuid4())

    @classmethod
    def deterministic(cls, *parts: str) -> Self:
        """Derive a stable identifier from a natural key.

        The same parts always produce the same identifier, in every process and
        every run. Useful where an entity has a genuine natural key -- an
        instrument is identified by exchange, symbol and expiry -- and where
        re-importing reference data must not mint duplicates.

        Parameters
        ----------
        *parts
            The natural key components, in a fixed order. Order matters and is
            the caller's responsibility to keep stable.

        Raises
        ------
        InvariantViolation
            If no parts are supplied, or any part is empty. An identifier derived
            from nothing would collide with every other identifier derived from
            nothing.
        """
        invariant(len(parts) > 0, "a deterministic identifier needs at least one part")
        invariant(
            all(part for part in parts),
            "deterministic identifier parts must not be empty",
            parts=list(parts),
        )
        return cls(uuid.uuid5(_DHRUVA_NAMESPACE, "\x1f".join(parts)))

    @classmethod
    def parse(cls, text: str, /) -> Self:
        """Parse the string form produced by :meth:`__str__`.

        Accepts both the prefixed form (``inst_<uuid>``) and a bare UUID, so that
        identifiers read straight from a database column parse without ceremony.

        Raises
        ------
        InvariantViolation
            If the text is not a valid identifier, or carries the wrong prefix.
            A wrong prefix means an identifier of one kind is being read as
            another, which is worth catching loudly.
        """
        candidate = text.strip()
        if "_" in candidate:
            prefix, _, remainder = candidate.partition("_")
            invariant(
                prefix == cls.PREFIX,
                "identifier prefix does not match this identifier type",
                text=text,
                expected_prefix=cls.PREFIX,
                actual_prefix=prefix,
            )
            candidate = remainder
        try:
            return cls(uuid.UUID(candidate))
        except ValueError as exc:
            msg = "identifier is not a valid UUID"
            raise _invalid(msg, text) from exc

    @property
    def value(self) -> uuid.UUID:
        """Return the underlying UUID. This is what is persisted."""
        return self._value

    def __eq__(self, other: object, /) -> bool:
        """Compare by type and value.

        Type participates deliberately: an ``AccountId`` and an ``InstrumentId``
        sharing a UUID are not the same thing, and treating them as equal would
        make a cross-type lookup succeed.
        """
        if type(other) is not type(self):
            return NotImplemented
        # `type(other) is type(self)` establishes the attribute exists, but the
        # checker narrows `object` only through isinstance, not through a type
        # identity comparison -- and isinstance would wrongly admit subclasses.
        equal: bool = self._value == other._value  # type: ignore[attr-defined]
        return equal

    def __hash__(self) -> int:
        """Hash by type and value, matching equality."""
        return hash((type(self).__name__, self._value))

    def __reduce__(self) -> tuple[type[SurrogateId], tuple[uuid.UUID]]:
        """Support pickling.

        Required explicitly because ``__slots__`` combined with the
        ``__setattr__`` override leaves pickle no way to restore state. Without
        this, identifiers could not cross a process boundary -- which they must,
        as soon as Celery tasks arrive in S05.
        """
        return (type(self), (self._value,))

    def __str__(self) -> str:
        """Return the prefixed string form, which is greppable in logs."""
        return f"{self.PREFIX}_{self._value}"

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return f"{type(self).__name__}('{self._value}')"


class InstrumentId(SurrogateId):
    """Identifies a tradable instrument.

    Never derived from a broker token or a trading symbol. Where a stable natural
    key exists, use :meth:`SurrogateId.deterministic` with exchange, symbol and
    expiry -- values the *exchange* defines, not values a broker assigns.
    """

    PREFIX: ClassVar[str] = "inst"


class AccountId(SurrogateId):
    """Identifies a trading account.

    Present from S03 although multi-tenancy activates at S44, because ADR-004
    requires ``account_id`` on every domain row from day one. Having the type
    early means no row has to be retrofitted.
    """

    PREFIX: ClassVar[str] = "acct"


def _invalid(message: str, text: str) -> InvariantViolation:
    """Build the error raised for an unparseable identifier."""
    return InvariantViolation(message, text=text)
