"""The argon2 password hasher (ADR-072, ADR-033).

The only module in the platform that imports a password-hashing library, for the
same reason ``crypto/envelope.py`` is the only one that imports a cipher: the
algorithm and its work factor are one decision, and a decision made in two places
is a decision that will eventually differ between them.

Why argon2id and not bcrypt or PBKDF2
-------------------------------------
Argon2id is memory-hard, which is the property that matters against the attack
that actually happens: an offline crack of a stolen table on rented GPUs. Bcrypt
resists GPUs less well and caps the password length at 72 bytes; PBKDF2 is
compute-hard only, so an attacker's advantage scales with hardware that gets
cheaper every year. The library's own defaults are used rather than tuned here --
they track current guidance, and a hand-picked work factor is a number that was
right on the day somebody typed it.

Why verification returns a bool
-------------------------------
A wrong password is an ordinary answer at this layer, not an exception. Turning
it into an authentication failure -- and making that failure indistinguishable
from an unknown subject -- is ADR-072's requirement and belongs to the use case,
which is also the thing that writes the audit record.

Why :meth:`verify_nothing` exists
---------------------------------
ADR-072 requires that "no such user" and "wrong password" be indistinguishable.
Returning the same error is half of that. The other half is the stopwatch: a
lookup that misses returns in microseconds while a real verification spends
deliberate milliseconds, and the difference is trivially measurable across a
handful of requests. That is a user-enumeration oracle that no error message
mentions, and it is the half people forget.

So when no principal matches, the use case still pays for a verification against
a hash of nothing. The cost is one wasted hash per failed login, which is exactly
the operation being deliberately made expensive anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import (
    Argon2Error,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from dhruva.contexts.platform.domain.identity.passwords import PasswordHash

if TYPE_CHECKING:
    from dhruva.shared.config.secret import SecretValue

__all__ = ["Argon2PasswordHasher"]

#: Hashed once at construction and verified against whenever no principal
#: matched. The value is irrelevant -- nothing may ever authenticate against it,
#: and it is a constant precisely so that it cannot accidentally be a password
#: somebody chose.
_NO_SUCH_PRINCIPAL: Final = "\x00 no principal matched \x00"


class Argon2PasswordHasher:
    """Hashes and verifies passwords with argon2id.

    Implements
    :class:`~dhruva.contexts.platform.domain.identity.passwords.PasswordHasher`
    structurally.

    Stateless apart from the underlying hasher's parameters, so one instance is
    safely shared. It is still injected rather than constructed at call sites,
    because the day the work factor is tuned should be a change in one wiring
    line rather than in every caller.
    """

    __slots__ = ("_dummy", "_hasher")

    def __init__(self, hasher: Argon2Hasher | None = None) -> None:
        """Bind to an argon2 hasher, defaulting to the library's parameters.

        The dummy hash is computed once here rather than per failed login. It
        costs one hash at startup and removes a per-request cost from the path
        that is already the slowest one.
        """
        self._hasher = hasher or Argon2Hasher()
        self._dummy = self._hasher.hash(_NO_SUCH_PRINCIPAL)

    def hash(self, password: SecretValue) -> PasswordHash:
        """Return an argon2id hash of ``password``.

        The salt is the library's, freshly generated per call and carried inside
        the encoded output. It is deliberately not a parameter: a caller that
        can supply a salt is a caller that can supply the same salt twice, which
        is how two identical passwords become visibly identical in a table.
        """
        return PasswordHash(self._hasher.hash(password.reveal()))

    def verify(self, password: SecretValue, expected: PasswordHash) -> bool:
        """Report whether ``password`` produced ``expected``.

        Notes
        -----
        Every argon2 failure mode collapses to ``False``, including a malformed
        stored hash. That is deliberate: from the caller's position "this
        password does not authenticate this principal" is the same answer
        whether the hash was wrong or unreadable, and distinguishing them would
        leak the state of the table. A stored hash that cannot be parsed is
        still a real defect -- it is visible in the audit log as a run of failed
        authentications for one subject, which is where an operator would look.
        """
        try:
            return self._hasher.verify(expected.encoded, password.reveal())
        except (
            VerifyMismatchError,
            InvalidHashError,
            VerificationError,
            Argon2Error,
        ):
            return False

    def verify_nothing(self, password: SecretValue) -> None:
        """Do the work of a verification and discard the result.

        Called when no principal matched, so that an unknown subject costs
        roughly what a wrong password costs. "Roughly" is honest: argon2's
        runtime is dominated by its memory-hard core, which this pays in full,
        but no equalisation is exact and this is a mitigation rather than a
        constant-time guarantee.
        """
        try:
            self._hasher.verify(self._dummy, password.reveal())
        except (
            VerifyMismatchError,
            InvalidHashError,
            VerificationError,
            Argon2Error,
        ):
            return
