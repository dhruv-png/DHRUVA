"""Collecting a secret from the operator without echoing it.

Thin, and deliberately so. The whole implementation is ``getpass`` plus one
refusal, and the refusal is the part worth having: ``getpass`` falls back to
visible input with a warning when it cannot find a terminal that hides
characters, which is precisely the situation where a fallback is worst -- a
piped or redirected session, where the "terminal" is a log file.
"""

from __future__ import annotations

import getpass
import sys
import warnings
from typing import TYPE_CHECKING

from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import UnsafeConfigurationError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["HiddenPrompt", "prompt_hidden"]


def prompt_hidden(prompt: str) -> SecretValue:
    """Read one secret from the terminal without displaying it.

    Raises
    ------
    UnsafeConfigurationError
        If the input channel cannot hide what is typed. ``getpass`` signals this
        with a ``GetPassWarning`` and then echoes anyway; this turns the warning
        into a refusal, because a secret typed into a recorded session is
        already compromised and continuing would only hide that fact.
    ValidationError
        If nothing was entered. An empty secret is far more likely to be a
        mistyped paste or an interrupted terminal than an intention, and sealing
        one produces a credential that fails at the broker with no clue why.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            entered = getpass.getpass(prompt)
        except getpass.GetPassWarning as error:
            message = (
                "this terminal cannot hide what you type, so the value would be "
                "echoed and recorded. Run the command in an interactive terminal."
            )
            raise UnsafeConfigurationError(message, field="interactive_input") from error

    value = entered.strip()
    if not value:
        message = "nothing was entered"
        raise ValidationError(message, field="interactive_input")
    return SecretValue(value)


class HiddenPrompt:
    """A :class:`SecretPrompt` that also refuses a non-interactive session.

    The class exists for the second check, which the function cannot make on its
    own: ``getpass`` reads ``/dev/tty`` where it can, so on some platforms it
    succeeds even when stdin is a pipe. Refusing when stdin is not a terminal
    means ``echo secret | dhruva-broker …`` fails instead of quietly working,
    and a flow that quietly works is one somebody will put in a script with the
    secret above it.
    """

    __slots__ = ("_isatty",)

    def __init__(self, *, isatty: Callable[[], bool] | None = None) -> None:
        """Bind to a terminal test, injectable so the refusal is testable."""
        self._isatty = isatty if isatty is not None else sys.stdin.isatty

    def __call__(self, prompt: str) -> SecretValue:
        """Return what the operator typed, or refuse.

        Raises
        ------
        UnsafeConfigurationError
            If stdin is not a terminal, or the terminal cannot hide input.
        ValidationError
            If nothing was entered.
        """
        if not self._isatty():
            message = (
                "secrets are only accepted from an interactive terminal. This "
                "command has no option for passing one on the command line, and "
                "piping one in would place it in shell history."
            )
            raise UnsafeConfigurationError(message, field="interactive_input")
        return prompt_hidden(prompt)
