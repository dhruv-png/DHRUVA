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

__all__ = [
    "AccountId",
    "CredentialId",
    "InstrumentId",
    "PrincipalId",
    "RefreshTokenId",
    "SurrogateId",
]

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

        Accepts any :class:`uuid.UUID`, including a driver's subclass of it, and
        stores a plain :class:`uuid.UUID`. Rejects everything else.

        Raises
        ------
        InvariantViolation
            If ``value`` is not a :class:`uuid.UUID`. Accepting a string here
            would make ``InstrumentId("NIFTY")`` succeed, which is exactly the
            broker-identifier coupling this class exists to prevent.

        Notes
        -----
        The check was originally ``type(value) is not uuid.UUID`` -- an exact
        type test. asyncpg does not return :class:`uuid.UUID`; it returns
        ``asyncpg.pgproto.pgproto.UUID``, a *subclass*. So every identifier read
        back from PostgreSQL was rejected, and the entire persistence layer could
        write rows it could never load. No unit test could see this: construct a
        ``uuid.UUID`` in Python and the exact check passes. It took a real
        database to produce a value of the real type, which is the case ADR-058
        makes in one line of evidence.

        ``isinstance`` is the correct test and loses nothing: ``str`` is not a
        UUID subclass, so ``InstrumentId("NIFTY")`` still raises.

        The subclass is then narrowed back to a plain ``uuid.UUID``. A domain
        identifier holding ``asyncpg.pgproto.UUID`` would put a driver type
        inside the domain model -- the exact leak the layering forbids -- and
        would make an identifier's type depend on whether it was constructed or
        loaded.
        """
        # Fast path first: the overwhelmingly common case is an exact uuid.UUID,
        # and identifiers are constructed on every row read from S04 onward.
        if type(value) is uuid.UUID:
            object.__setattr__(self, "_value", value)
            return
        if isinstance(value, uuid.UUID):
            object.__setattr__(self, "_value", uuid.UUID(int=value.int))
            return
        raise InvariantViolation(
            f"{type(self).__name__} requires a UUID",
            value=repr(value),
            actual_type=type(value).__name__,
        )

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


class CredentialId(SurrogateId):
    """Identifies one stored broker credential (ADR-070, S06).

    Unlike the worked example's surrogate row key, this identifier is **domain
    identity, not a persistence detail**, and the distinction is load-bearing:
    ADR-070's associated data binds a credential's ciphertext to the record it
    belongs to, and a binding to a value the domain refuses to know would be a
    binding nothing could verify on read.

    Minted, never derived. :meth:`SurrogateId.deterministic` is deliberately not
    used here even though ``(account_id, broker)`` is a genuine natural key:
    deriving the identifier from the natural key would make the associated data
    a function of the same two facts it is supposed to bind independently, and
    re-adding a deleted credential would silently reuse the identifier of the
    one it replaced.
    """

    PREFIX: ClassVar[str] = "cred"


class PrincipalId(SurrogateId):
    """Identifies one authenticating principal -- today, a human operator (ADR-072).

    Distinct from :class:`AccountId`, and the distinction is the one most easily
    lost in a single-operator system where the two happen to be one-to-one. An
    account is a **tenant**: the thing ADR-004 scopes every row to. A principal
    is **who acted**: the thing an audit record names and an access token asserts
    as its subject. Collapsing them would make "which human did this?"
    unanswerable the moment a second operator exists, and would have to be
    unpicked across every table that had meanwhile stored one meaning under the
    other's name.

    Minted, never derived. A principal's natural key is its login subject, which
    is exactly the value most likely to change -- an operator's email address is
    not a stable identity, and deriving from it would rename the principal every
    time the address did.
    """

    PREFIX: ClassVar[str] = "prin"


class RefreshTokenId(SurrogateId):
    """Identifies one issued refresh token (ADR-072).

    A refresh token is a row rather than a signature precisely so that it can be
    revoked and so that its lineage can be walked, and both require it to have an
    identity independent of the secret it represents. The identifier is safe to
    log; the token itself never is, and only its hash is stored.
    """

    PREFIX: ClassVar[str] = "rtok"


def _invalid(message: str, text: str) -> InvariantViolation:
    """Build the error raised for an unparseable identifier."""
    return InvariantViolation(message, text=text)
