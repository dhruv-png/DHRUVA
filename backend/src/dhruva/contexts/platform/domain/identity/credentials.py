"""What a stored broker credential is (ADR-070, ADR-020, ADR-004, ADR-057).

Pure domain. No cipher, no session, no framework -- this module states what a
credential *is* and what its ciphertext is bound to, and it can do neither
encryption nor storage.

Why the sealed value object lives here and not beside the cipher
----------------------------------------------------------------
:class:`EncryptedSecret` was introduced in the S06 envelope-encryption commit
under ``infrastructure.crypto``, which was the right home while nothing but the
cipher used it. The credential store changes that: ADR-052 requires a domain
aggregate, ADR-070 requires that aggregate to hold ciphertext rather than a
plaintext, and a domain class holding an infrastructure class is precisely the
import the Clean Architecture layer contract exists to fail.

So the definition moved down a layer rather than the dependency being inverted
around it. ``infrastructure.crypto`` re-exports the name, so nothing that
already imports :class:`EncryptedSecret` from there had to change, and the
cipher module keeps depending on the domain -- which is the direction that is
allowed.

Why a credential carries its own identity
------------------------------------------
The worked example treats its surrogate primary key as a persistence concern the
domain refuses to know, and for that aggregate it is right: a snapshot is
identified by instrument and trading day.

A credential cannot afford the same stance. ADR-070's associated data binds a
ciphertext to *the record it belongs to*, and TD-S06-6 says the binding needs a
stable record identity. If the identity were minted inside the repository and
discarded, the binding computed on write could not be recomputed on read, and
the whole construction would be unverifiable. ADR-009 already makes a minted
UUID the normal way to identify an entity here, so this is the ordinary case and
the worked example is the exception.

Why `broker` is a plain string
-------------------------------
ADR-003 makes the platform provider-agnostic, and no accepted decision
enumerates the brokers v1 supports. An enum here would record a decision nobody
has taken, in the module that would be hardest to change later. The same
reasoning ADR-071's record applied to ``actor``: a string is the honest
representation of *not yet decided*. The invariants below constrain its shape
so that it is still safe to bind cryptographically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import AccountId, CredentialId

__all__ = [
    "BROKER_MAX_LENGTH",
    "Credential",
    "EncryptedSecret",
    "credential_associated_data",
]

#: Matches ``credential.broker VARCHAR(32)``. Declared here as well as in the
#: migration because a value that fits the domain and not the column would fail
#: at the driver, which is the least informative place to learn it.
BROKER_MAX_LENGTH: Final = 32

#: Field separator for the associated data below. ASCII unit separator, chosen
#: because :func:`_valid_broker` forbids it in a broker name and neither the
#: identifier form nor a UUID can contain it. A separator that a field could
#: contain would let two different credentials produce identical associated
#: data, which is the one thing this construction must not allow.
_SEPARATOR: Final = b"\x1f"

#: Version tag on the associated data. If the encoding below ever changes, every
#: existing ciphertext must fail to open rather than open under a binding that
#: means something different -- and a tag is what makes that failure certain
#: instead of accidental.
_AAD_SCHEME: Final = b"dhruva.credential.aad.v1"

#: Every character a broker name may contain, written out.
#:
#: Enumerated rather than tested with :meth:`str.isalnum`, which was the first
#: attempt and was wrong twice over. ``isalnum`` is true of ``"Zerodha"``, so the
#: canonical-case rule this module documents was not actually enforced -- two
#: spellings of one broker would have produced two bindings, and a credential
#: written under one could not be read under the other. It is also true of Thai
#: digits and Roman numerals, so the ASCII guard beside it was doing load-bearing
#: work that was easy to miss.
#:
#: A literal alphabet cannot drift from what the docstring claims, and a reader
#: can check it in one glance rather than reasoning about Unicode categories.
_BROKER_ALPHABET: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _valid_broker(broker: str) -> bool:
    """Report whether ``broker`` is a safe, canonical broker name.

    Lowercase ASCII letters, digits, hyphen and underscore, and at least one of
    them. Restrictive on purpose: this value is bound into associated data, so
    two spellings of one broker would produce two bindings, and a credential
    written under one spelling could not be read under the other.
    """
    return bool(broker) and _BROKER_ALPHABET.issuperset(broker)


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """A sealed credential and the wrapped key that opens it (ADR-070).

    Exactly the two values ADR-070 says a row stores. Neither field is
    meaningful without a ``KeyProvider``, and neither reveals anything about the
    plaintext beyond its approximate length.

    Frozen because these two values are only correct together: a ciphertext
    paired with a re-wrapped key from another record decrypts to nothing, and
    making that unrepresentable is cheaper than testing for it.
    """

    ciphertext: bytes
    wrapped_data_key: bytes


def credential_associated_data(
    credential_id: CredentialId, account_id: AccountId, broker: str
) -> bytes:
    """Return the AES-GCM associated data binding a ciphertext to its row.

    This closes TD-S06-6. Associated data is authenticated but not encrypted, so
    a ciphertext sealed under this value opens only when the same three facts are
    presented again on read. Lifting row A's ``ciphertext`` and
    ``wrapped_data_key`` into row B -- the attack the debt named -- then produces
    a decryption failure rather than row A's secret under row B's name.

    Parameters
    ----------
    credential_id, account_id, broker
        The three facts a credential is bound to. ``account_id`` is included
        even though ``credential_id`` alone is unique, because ADR-004 makes the
        tenant a property of every row and a credential re-parented to another
        account is exactly the tampering worth refusing.

    Returns
    -------
    bytes
        A canonical, unambiguous encoding. Deterministic across processes and
        runs: it must be, or a credential written by one process could not be
        read by another.

    Notes
    -----
    The identifiers contribute their prefixed string form rather than a bare
    UUID, so an ``AccountId`` and a ``CredentialId`` that happened to share a
    UUID could not produce the same binding.

    Only the credential's ciphertext is sealed under this value; the wrapped
    data key is not. That is deliberate. Binding the wrap too would mean widening
    the :class:`~dhruva.contexts.platform.domain.identity.keys.KeyProvider` port,
    and ADR-070 argues for that port precisely so a KMS adapter is a later
    substitution requiring no domain change -- while real KMS APIs express
    encryption context as a string map rather than as bytes. It also buys
    nothing: a wrapped data key on its own opens nothing, and a ciphertext moved
    without its key is already refused by GCM.
    """
    invariant(
        _valid_broker(broker),
        "broker must be lowercase alphanumeric with hyphen or underscore",
        broker=broker,
    )
    parts = (_AAD_SCHEME, str(credential_id).encode(), str(account_id).encode(), broker.encode())
    return _SEPARATOR.join(parts)


@dataclass(frozen=True, slots=True)
class Credential:
    """One broker's credential for one account, held sealed.

    There is no accessor returning a plaintext, and there cannot be one: this
    object holds no key material and imports no cipher. Opening it is a separate
    call taking a ``KeyProvider``, which is what ADR-070 requires and what makes
    the plaintext path greppable.

    Attributes
    ----------
    credential_id
        Domain identity, and the anchor of the associated data. See the module
        docstring for why this aggregate carries one when the worked example
        does not.
    account_id
        The tenant this credential belongs to (ADR-004). One credential per
        broker per account, which the table enforces as a unique constraint.
    broker
        Which broker the secret authenticates against.
    secret
        The sealed material. Ciphertext and wrapped data key, never a plaintext.
    key_version
        Which master key wrapped ``secret``'s data key. S42's rotation job reads
        this to find rows still wrapped under a superseded key (TD-S06-4).
    created_at, updated_at
        When the row was first written and last written. Both timezone-aware
        (ADR-006).
    rotated_at
        When the secret was last re-sealed, or ``None`` if it never has been.
        Distinct from ``updated_at``, which any write moves.
    version
        Optimistic-concurrency token (ADR-057). Carried by the domain object
        because the caller needs to see the conflict, not because the domain
        cares how rows are locked.
    """

    credential_id: CredentialId
    account_id: AccountId
    broker: str
    secret: EncryptedSecret
    created_at: datetime
    updated_at: datetime
    key_version: int = 1
    rotated_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        """Reject a credential that could not describe a storable, openable row."""
        invariant(
            _valid_broker(self.broker),
            "broker must be lowercase alphanumeric with hyphen or underscore",
            broker=self.broker,
        )
        invariant(
            len(self.broker) <= BROKER_MAX_LENGTH,
            "broker name is too long to store",
            broker=self.broker,
            maximum=BROKER_MAX_LENGTH,
        )
        # An empty ciphertext cannot have come from the envelope: every sealed
        # value carries a nonce and a tag even when the plaintext was empty.
        # Refusing it here means a row that was never sealed cannot masquerade
        # as one that was.
        invariant(
            bool(self.secret.ciphertext) and bool(self.secret.wrapped_data_key),
            "a credential must hold sealed material",
            broker=self.broker,
        )
        invariant(
            self.key_version >= 1,
            "key_version starts at one",
            key_version=self.key_version,
        )
        invariant(self.version >= 1, "version starts at one", version=self.version)
        for name in ("created_at", "updated_at", "rotated_at"):
            value: datetime | None = getattr(self, name)
            invariant(
                value is None or (value.tzinfo is not None and value.utcoffset() is not None),
                f"{name} must be timezone-aware",
                field=name,
            )
        invariant(
            self.updated_at >= self.created_at,
            "updated_at cannot precede created_at",
            created_at=self.created_at.isoformat(),
            updated_at=self.updated_at.isoformat(),
        )

    @property
    def associated_data(self) -> bytes:
        """Return the binding this credential's ciphertext was sealed under.

        A property rather than a stored field, so it cannot drift from the three
        facts it is derived from. A stored copy that disagreed with the row would
        be a binding that verifies nothing.
        """
        return credential_associated_data(self.credential_id, self.account_id, self.broker)

    def resealed(
        self, secret: EncryptedSecret, *, at: datetime, key_version: int | None = None
    ) -> Credential:
        """Return a copy holding new sealed material, one version on.

        A new object rather than a mutation, matching every other aggregate here:
        the caller keeps the version it loaded at, which is what
        ``repository.update`` matches on to detect a lost update (ADR-057).

        Parameters
        ----------
        secret
            The re-sealed material. Must have been sealed under
            :attr:`associated_data`; nothing in the domain can check that,
            because checking requires decrypting, and the failure surfaces as a
            refusal on the next read rather than as corruption.
        at
            When the rotation happened. Injected, never read from the wall clock
            (ADR-011).
        key_version
            Supply this only when the master key changed. Left out, the existing
            value is kept -- re-sealing a secret under the same master key is not
            a key rotation and should not look like one to S42's job.

        Returns
        -------
        Credential
            The rotated credential, with ``version`` incremented.
        """
        invariant(
            at.tzinfo is not None and at.utcoffset() is not None,
            "rotation time must be timezone-aware",
        )
        invariant(
            at >= self.updated_at,
            "a rotation cannot predate the last write",
            updated_at=self.updated_at.isoformat(),
            at=at.isoformat(),
        )
        return replace(
            self,
            secret=secret,
            updated_at=at,
            rotated_at=at,
            key_version=self.key_version if key_version is None else key_version,
            version=self.version + 1,
        )
