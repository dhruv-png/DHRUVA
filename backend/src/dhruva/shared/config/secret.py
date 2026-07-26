"""A string that refuses to reveal itself.

:class:`SecretValue` is the platform's in-memory representation of any bearer
credential: the Kite API secret, a daily access token, a database password.

The threat it addresses is not an attacker -- it is an ordinary Tuesday. A
developer logs a settings object. An exception carries a connection string in its
arguments. A traceback renders local variables. In each case the credential
reaches a log sink through a path no policy anticipated, and redaction by field
name does not help because the credential is not in a field called ``password``;
it is inside a rendered string.

A value that cannot render itself closes that path structurally (ADR-033).
"""

from __future__ import annotations

import hmac
from typing import Final

__all__ = ["REDACTED_PLACEHOLDER", "SecretValue", "registered_secret_values"]

#: What a redacted value renders as, everywhere. Distinctive on purpose: it is
#: greppable in a log archive, and unmistakable in a screenshot.
REDACTED_PLACEHOLDER: Final = "«redacted»"

#: Minimum length for a secret to join the value-scanning registry. Short strings
#: would cause false-positive redaction of ordinary log content -- redacting the
#: word "test" out of every message would be worse than the leak it prevents.
_MIN_REGISTERED_LENGTH: Final = 8

#: Process-local registry of secret values, consulted by the logging redaction
#: processor. Deliberately a plain set: it is small, single-digit in practice, and
#: lives only in memory.
_REGISTRY: set[str] = set()


def registered_secret_values() -> frozenset[str]:
    """Return the secret values currently registered for value-based redaction.

    Returns
    -------
    frozenset[str]
        A snapshot. The logging processor takes one per configuration rather
        than per record, so this is not on the hot path.

    Notes
    -----
    This function returns actual secret values, which is unavoidable -- the
    redaction processor needs them. It is deliberately named so that every call
    site is obvious in review and in ``grep``.
    """
    return frozenset(_REGISTRY)


class SecretValue:
    """A credential that never renders itself.

    ``repr``, ``str``, f-string interpolation, ``format`` and JSON serialisation
    all produce :data:`REDACTED_PLACEHOLDER`. The only way to obtain the
    underlying string is :meth:`reveal`, which is named so that its call sites can
    be found and reviewed.

    Examples
    --------
    >>> token = SecretValue("kite-access-token-abc123")
    >>> str(token)
    '«redacted»'
    >>> f"connecting with {token}"
    'connecting with «redacted»'
    >>> token.reveal()
    'kite-access-token-abc123'
    >>> SecretValue("kite-access-token-abc123") == token
    True

    Notes
    -----
    Equality is supported because configuration is compared in tests, and it is
    constant-time to avoid leaking length or prefix information through timing.
    Ordering and hashing by value are deliberately **not** supported: a hashable
    secret ends up as a dictionary key, and dictionary keys end up in logs.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str, /, *, register: bool = True) -> None:
        """Wrap ``value``.

        Parameters
        ----------
        value
            The credential.
        register
            Whether to add the value to the redaction registry so the logging
            processor can scrub it out of rendered strings. Defaults to ``True``.
            Pass ``False`` only for values that are not really secret, such as
            test fixtures whose appearance in output is expected.
        """
        self._value = value
        if register and len(value) >= _MIN_REGISTERED_LENGTH:
            _REGISTRY.add(value)

    def reveal(self) -> str:
        """Return the underlying credential.

        Every call site of this method is a place where a secret enters plain
        string handling, and should be short-lived and reviewed.
        """
        return self._value

    def __str__(self) -> str:
        """Return the redaction placeholder."""
        return REDACTED_PLACEHOLDER

    def __repr__(self) -> str:
        """Return the redaction placeholder, typed."""
        return f"SecretValue({REDACTED_PLACEHOLDER})"

    def __format__(self, format_spec: str, /) -> str:
        """Return the redaction placeholder, ignoring any format specification.

        The specification is ignored on purpose: ``f"{secret:.4}"`` must not
        become a way to leak a prefix.
        """
        return REDACTED_PLACEHOLDER

    def __bool__(self) -> bool:
        """Return whether a non-empty credential is held."""
        return bool(self._value)

    def __len__(self) -> int:
        """Return the credential's length.

        Length is disclosed deliberately: validation needs it, and it is not
        sensitive in the way the value is.
        """
        return len(self._value)

    def __eq__(self, other: object, /) -> bool:
        """Compare in constant time against another secret.

        Comparison with a plain :class:`str` returns ``NotImplemented`` rather
        than comparing, so that an accidental ``settings.token == "abc"`` is a
        type error in review instead of a silently working timing oracle.
        """
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(self._value, other._value)

    def __hash__(self) -> int:
        """Refuse to be hashed.

        Raises
        ------
        TypeError
            Always. A hashable secret becomes a dictionary key, and dictionary
            keys end up in logs.
        """
        msg = "SecretValue is not hashable; a secret must never become a dict key"
        raise TypeError(msg)

    def __getstate__(self) -> object:
        """Refuse to be pickled.

        Raises
        ------
        TypeError
            Always. Pickling a credential writes it to disk or to a queue, which
            is the outcome this class exists to prevent.
        """
        msg = "SecretValue cannot be serialised; pass the configuration provider instead"
        raise TypeError(msg)
