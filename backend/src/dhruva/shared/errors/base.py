"""The base of the error taxonomy.

Every error raised deliberately by D.H.R.U.V.A derives from :class:`DhruvaError`
and carries three things a bare exception does not:

* a **stable machine-readable code** that means the same thing in 2028;
* **structured context** as fields rather than as an interpolated message, so it
  lands in logs as queryable data;
* a **retryability flag**, so callers do not have to infer it from the class name.

See ADR-038 for why the taxonomy is closed and the codes are pinned by test.
"""

from __future__ import annotations

from typing import Any, Self

__all__ = ["DhruvaError", "ErrorCode"]


class ErrorCode(str):
    """A stable, machine-readable error identifier of the form ``DHR-XXX-NNN``.

    Subclasses :class:`str` so it serialises and compares as one, while giving the
    format a name and a validation point. Codes are part of the platform's public
    contract: they appear in logs, in API responses, and eventually in alert rules,
    so renaming one is a breaking change and is caught by a pinning test.
    """

    __slots__ = ()

    _EXPECTED_PARTS = 3
    _FAMILY_LENGTH = 3
    _NUMBER_LENGTH = 3

    def __new__(cls, value: str) -> Self:
        """Create a code, rejecting anything that does not match ``DHR-XXX-NNN``.

        Raises
        ------
        ValueError
            If the format is wrong. Failing at import time is deliberate: a
            malformed code should never reach a log line.
        """
        parts = value.split("-")
        if (
            len(parts) != cls._EXPECTED_PARTS
            or parts[0] != "DHR"
            or len(parts[1]) != cls._FAMILY_LENGTH
            or not parts[1].isalpha()
            or not parts[1].isupper()
            or len(parts[2]) != cls._NUMBER_LENGTH
            or not parts[2].isdigit()
        ):
            msg = f"error code {value!r} must match 'DHR-XXX-NNN', e.g. 'DHR-CFG-001'"
            raise ValueError(msg)
        return super().__new__(cls, value)

    @property
    def family(self) -> str:
        """Return the three-letter family, for example ``CFG``."""
        return self.split("-")[1]


class DhruvaError(Exception):
    """Base of every error the platform raises deliberately.

    Subclasses set :attr:`code` and, where appropriate, :attr:`retryable`.
    Construction takes a message and arbitrary keyword context; the context is
    what makes the resulting log record useful.

    Examples
    --------
    >>> class StaleDataError(DhruvaError):
    ...     code = ErrorCode("DHR-DQL-001")
    >>> err = StaleDataError("tick data is stale", instrument_id="NIFTY", age_seconds=42)
    >>> err.context["age_seconds"]
    42
    >>> str(err)
    '[DHR-DQL-001] tick data is stale'

    Notes
    -----
    Context values are stored as given and are **not** rendered into the message.
    This is deliberate: an interpolated message is a string a machine cannot
    filter on, and it is also the most common way a credential ends up in a log
    line (ADR-033).
    """

    #: Overridden by every concrete subclass.
    code: ErrorCode = ErrorCode("DHR-GEN-001")

    #: Whether retrying the same operation could plausibly succeed. Callers use
    #: this instead of matching on exception types they do not own.
    retryable: bool = False

    def __init__(self, message: str, /, **context: Any) -> None:
        """Initialise the error.

        Parameters
        ----------
        message
            Human-readable, stable across invocations. Must not interpolate
            values -- pass those as context instead.
        **context
            Structured fields describing this occurrence.
        """
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        """Return the message prefixed with the code, as it appears in logs."""
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        """Return an unambiguous representation including the context keys.

        Context *values* are omitted here because ``repr`` is what debuggers and
        some logging paths call implicitly, and a value could be a credential.
        The keys are enough to identify the error; :meth:`to_dict` is the
        deliberate, greppable way to obtain the values.
        """
        keys = ", ".join(sorted(self.context)) or "-"
        return f"{type(self).__name__}(code={self.code!s}, context_keys=[{keys}])"

    def to_dict(self) -> dict[str, Any]:
        """Render the error as a structured mapping for logs and API responses.

        Returns
        -------
        dict[str, Any]
            ``error``, ``code``, ``message``, ``retryable`` and ``context``.
        """
        return {
            "error": type(self).__name__,
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
            "context": dict(self.context),
        }
