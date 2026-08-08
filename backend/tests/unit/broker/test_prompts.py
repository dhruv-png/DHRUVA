"""The hidden prompt, and the two situations in which it refuses.

Small surface, and every test here is about a refusal. That is the point of the
component: ``getpass`` on its own degrades to visible input rather than failing,
and degrading is the wrong behaviour for a value whose whole security property
is that it was never displayed.
"""

from __future__ import annotations

import getpass
import warnings
from typing import Final

import pytest

from dhruva.contexts.platform.infrastructure.prompts import HiddenPrompt, prompt_hidden
from dhruva.shared.errors import UnsafeConfigurationError, ValidationError

pytestmark = pytest.mark.unit

#: Obviously synthetic.
ENTERED: Final = "synthetic-typed-material"


def test_what_the_operator_types_is_returned_as_a_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapped rather than bare, so the redaction stack knows about it (ADR-037)."""
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": ENTERED)

    value = prompt_hidden("secret: ")

    assert value.reveal() == ENTERED
    assert ENTERED not in repr(value)


def test_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pasted value usually arrives with a trailing newline or a space.

    Sealing that would produce a credential the broker rejects, and the operator
    would have no way to see the difference between what they pasted and what
    was stored.
    """
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": f"  {ENTERED}\t")

    assert prompt_hidden("secret: ").reveal() == ENTERED


@pytest.mark.parametrize("typed", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_entering_nothing_is_refused(monkeypatch: pytest.MonkeyPatch, typed: str) -> None:
    """Almost always an interrupted paste rather than an intention.

    Sealing an empty secret produces a credential that fails at the broker with
    an error that says nothing about the real cause.
    """
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": typed)

    with pytest.raises(ValidationError):
        prompt_hidden("secret: ")


def test_a_terminal_that_cannot_hide_input_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """``getpass`` warns and then echoes; this turns the warning into a refusal.

    The situation it fires in -- no controlling terminal, output redirected -- is
    exactly the one where echoing is worst, because the "screen" is a file
    somebody keeps.
    """

    def warn_then_echo(_prompt: str = "") -> str:
        warnings.warn("Can not control echo on the terminal", getpass.GetPassWarning, stacklevel=1)
        return ENTERED

    monkeypatch.setattr(getpass, "getpass", warn_then_echo)

    with pytest.raises(UnsafeConfigurationError, match="cannot hide"):
        prompt_hidden("secret: ")


def test_a_non_interactive_session_is_refused_before_anything_is_read() -> None:
    """``echo secret | dhruva-broker …`` must fail rather than quietly work.

    A pipeline that works is one somebody puts in a script, with the secret on
    the line above it, and that script ends up in version control. Refusing is
    the only outcome that does not eventually produce a committed credential.
    """
    prompt = HiddenPrompt(isatty=lambda: False)

    with pytest.raises(UnsafeConfigurationError, match="interactive terminal"):
        prompt("secret: ")


def test_an_interactive_session_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal must not be so broad that the command cannot be used."""
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": ENTERED)
    prompt = HiddenPrompt(isatty=lambda: True)

    assert prompt("secret: ").reveal() == ENTERED


def test_the_prompt_module_never_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is echoed back after entry, not even a confirmation.

    "Stored: synthetic-…" would be a helpful-looking line that puts the secret
    on screen, which is precisely what the hidden prompt was for.
    """
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": ENTERED)
    written: list[str] = []

    def record(text: str) -> int:
        written.append(text)
        return len(text)

    monkeypatch.setattr("sys.stdout.write", record)

    prompt_hidden("secret: ")

    assert ENTERED not in "".join(written)


def test_the_refusal_names_no_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """An error message is the most-copied text in any incident."""

    def warn_then_echo(_prompt: str = "") -> str:
        warnings.warn("Can not control echo on the terminal", getpass.GetPassWarning, stacklevel=1)
        return ENTERED

    monkeypatch.setattr(getpass, "getpass", warn_then_echo)

    with pytest.raises(UnsafeConfigurationError) as caught:
        prompt_hidden("secret: ")

    assert ENTERED not in f"{caught.value}{caught.value.context}"
