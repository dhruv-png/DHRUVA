"""``dhruva-broker`` -- the surface, not the plumbing.

What is asserted here is almost entirely negative, because the value of this
command is in what it cannot do. It cannot accept a secret as an argument, it
cannot reach an order-placement code path, and it cannot print a token. Each of
those is one careless edit away from being false, so each has a test that reads
the module rather than trusting its docstring.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
from typing import TYPE_CHECKING

import pytest

import dhruva.workers.broker as broker_module
from dhruva.workers.broker import build_parser

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

#: Anything an option name must never contain. Substrings rather than exact
#: names on purpose: ``--kite-api-secret`` and ``--secret`` are the same mistake.
_FORBIDDEN_IN_OPTIONS = (
    "secret",
    "token",
    "password",
    "passwd",
    "pin",
    "totp",
    "otp",
    "key",
    "credential",
)


def _all_actions(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    """Walk every action, including those inside every subparser."""
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _all_actions(sub)


def test_no_option_anywhere_can_carry_a_secret() -> None:
    """The guarantee the whole command rests on, asserted on the parser itself.

    Adding ``--api-secret`` is the single change that would let an API secret
    into PowerShell history, a process list and a CI log at once. This test is
    what makes that change fail rather than merely be discouraged.
    """
    names = [option for action in _all_actions(build_parser()) for option in action.option_strings]

    assert names, "the parser exposes no options at all, which is suspicious"
    for name in names:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IN_OPTIONS:
            assert forbidden not in lowered, f"{name} could carry a secret"


def test_the_only_options_are_help_and_account() -> None:
    """Stated exactly, so a new option is a decision rather than an accident.

    The substring test above is the safety net; this is the intent. A future
    ``--redirect-url`` would fail here and have to be argued for, which is the
    right amount of friction for a command that holds credentials.
    """
    names = {option for action in _all_actions(build_parser()) for option in action.option_strings}

    assert names == {"-h", "--help", "--account"}


def test_every_subcommand_requires_an_account() -> None:
    """An account-less default would write one operator's credential as another."""
    parser = build_parser()

    for action in _all_actions(parser):
        if "--account" in action.option_strings:
            assert action.required is True


@pytest.mark.parametrize(
    "argv",
    [
        ["zerodha", "enrol", "--account", "owner-family", "--api-secret", "x"],
        ["zerodha", "login", "--account", "owner-family", "--request-token", "x"],
        ["zerodha", "login", "--account", "owner-family", "--access-token", "x"],
        ["zerodha", "enrol", "--account", "owner-family", "--totp", "123456"],
    ],
    ids=["api-secret", "request-token", "access-token", "totp"],
)
def test_passing_a_secret_on_the_command_line_is_refused(argv: list[str]) -> None:
    """Not ignored -- refused. An ignored option would look like it worked.

    ``argparse`` exits with status 2 on an unknown option, which is the correct
    outcome: the operator sees the refusal before the secret is used, and knows
    to clear it from their shell history.
    """
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(argv)

    assert caught.value.code == 2


def test_the_three_subcommands_are_enrol_login_and_status() -> None:
    """Stated so that a fourth -- ``fetch``, say -- is a deliberate act."""
    parser = build_parser()
    subcommands: set[str] = set()
    for action in _all_actions(parser):
        if isinstance(action, argparse._SubParsersAction):
            subcommands |= set(action.choices)

    assert subcommands == {"zerodha", "enrol", "login", "status"}


def test_the_safety_notice_states_what_the_command_will_not_do() -> None:
    """Printed on ``--help``, and the first thing a nervous operator reads."""
    epilog = build_parser().epilog or ""

    for promise in ("no order placement", "no market-data fetch", "hidden prompt"):
        assert promise in epilog


def test_the_command_imports_no_order_or_trading_module() -> None:
    """The credential-holding command must not be adjacent to execution.

    Parsed rather than grepped: the module's own docstring says the word
    "orders", and a guard tripped by its own denial gets deleted rather than
    respected.
    """
    tree = ast.parse(pathlib.Path(broker_module.__file__ or "").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    joined = " ".join(imported).lower()
    for forbidden in ("order", "trading", "gtt", "portfolio", "position", "holding", "execution"):
        assert forbidden not in joined


def test_the_command_never_writes_a_revealed_secret() -> None:
    """No ``.reveal()`` anywhere in the command module.

    The strongest available check short of running every branch: the only way a
    secret reaches stdout from here is by being unwrapped first, and this module
    has no legitimate reason to unwrap one -- the use cases do that behind the
    boundary.
    """
    source = pathlib.Path(broker_module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)

    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "reveal" not in calls
