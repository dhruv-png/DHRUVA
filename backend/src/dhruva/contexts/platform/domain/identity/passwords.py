"""What a password hash is, and the port that produces one (ADR-072, ADR-033).

Pure domain. No hashing library is imported here and none may be: the algorithm,
its parameters and its encoded form belong to exactly one adapter, so that
changing them is one file rather than an archaeology exercise.

Why a type rather than a ``str``
--------------------------------
:class:`PasswordHash` exists to make one specific catastrophe unrepresentable:
constructing a principal with a **plaintext password** in the field meant for its
hash. As a bare ``str`` that is a silent, type-checking, test-passing mistake
whose consequence is a database of plaintext passwords, discovered by someone
else. As a distinct type it does not compile.

That is the same dimensional argument ADR-043 makes for ``Money`` versus
``Price``, and the same one :class:`~dhruva.shared.identity.SurrogateId` makes
for identifiers that share a representation. It costs a class and a field access;
it buys a whole class of breach.

Why the hash is not a ``SecretValue``
-------------------------------------
:class:`~dhruva.shared.config.secret.SecretValue` exists for values that are
**recoverable and must not leak**. A password hash is neither: it is one-way by
construction, so leaking it does not disclose the password, and registering every
principal's hash in the global redaction set would grow that set with values
nobody can misuse anyway.

It is still not something to print. :meth:`PasswordHash.__str__` redacts, so a
hash reaching a log line through the ordinary route shows a marker rather than
argon2 parameters that tell an attacker exactly how much work a crack would be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from dhruva.shared.config.secret import SecretValue

__all__ = ["REDACTED_HASH", "PasswordHash", "PasswordHasher"]

#: What a hash renders as. Matches the shape of ``SecretValue``'s marker so a
#: reader of a log line recognises the category without having to know which of
#: the two produced it.
REDACTED_HASH: Final = "<password-hash>"


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """The encoded output of a one-way password hash.

    Opaque by design. The domain never parses ``encoded``, never compares two
    hashes for equality as a way of verifying a password, and never decides what
    algorithm produced it -- all three belong to :class:`PasswordHasher`, and a
    domain that knew any of them would be a second place the algorithm was
    chosen.

    Attributes
    ----------
    encoded
        The adapter's self-describing form. Argon2's encoded string carries its
        algorithm, parameters and salt inline, which is why a verification needs
        nothing but this value and the password -- and why rotating the work
        factor does not invalidate existing hashes.
    """

    encoded: str

    def __post_init__(self) -> None:
        """Reject an empty hash.

        A blank hash is not a hash of a blank password; it is a field somebody
        failed to fill in. Allowing it would mean a principal that no password
        can authenticate and none can be verified against -- which fails, but
        only at the login attempt, and looks like a forgotten password.
        """
        invariant(bool(self.encoded.strip()), "a password hash cannot be blank")

    def __str__(self) -> str:
        """Return the redaction marker, never the hash."""
        return REDACTED_HASH

    def __repr__(self) -> str:
        """Return the redaction marker, for the same reason as ``__str__``.

        ``repr`` is what a debugger and several logging paths call implicitly,
        so leaving it as the dataclass default would put argon2 parameters into
        the exact places nobody thought to check.
        """
        return f"PasswordHash({REDACTED_HASH})"


@runtime_checkable
class PasswordHasher(Protocol):
    """Hashes and verifies passwords (ADR-072, ADR-033).

    One implementation today, in ``infrastructure.identity``. The port exists so
    that the work factor and the algorithm are an adapter concern, and so that
    application code cannot be written against a specific library's exception
    types.
    """

    def hash(self, password: SecretValue) -> PasswordHash:
        """Return a hash of ``password``.

        Salting is the implementation's responsibility and is not a parameter.
        A caller-supplied salt is a caller that can supply the same salt twice.
        """
        ...

    def verify(self, password: SecretValue, expected: PasswordHash) -> bool:
        """Report whether ``password`` produced ``expected``.

        Returns a ``bool`` rather than raising, because at this layer a wrong
        password is an ordinary answer. The decision to treat it as an
        authentication failure -- and to make it indistinguishable from an
        unknown subject -- belongs to the use case (ADR-072).
        """
        ...

    def verify_nothing(self, password: SecretValue) -> None:
        """Do the work of a verification and discard the result.

        Called when no principal matched, so that an unknown subject costs the
        same wall-clock time as a wrong password. Without it, ADR-072's
        requirement that the two be indistinguishable holds for the error code
        and fails for the stopwatch -- and a timing oracle enumerates users just
        as well as an error message does, more quietly.
        """
        ...
