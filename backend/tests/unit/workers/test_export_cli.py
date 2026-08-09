"""``dhruva-export`` refuses to destroy a snapshot, and shares the digest's rules.

Two things matter here. The overwrite behaviour is fail-closed, because a
snapshot is what somebody keeps in order to be able to say what they knew. And
the argument rules are literally the same functions ``dhruva-digest`` uses, so
the two cannot come to disagree about what a cutoff means.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from dhruva.contexts.intelligence.interfaces.digest_export import EXPORT_SCHEMA_VERSION
from dhruva.contexts.intelligence.interfaces.research_packet import PACKET_SCHEMA_VERSION
from dhruva.contexts.marketdata.api import DEFAULT_MULTI_DAY_SESSIONS
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId
from dhruva.workers import digest as digest_cli
from dhruva.workers import export_snapshot as cli
from dhruva.workers.cli_arguments import (
    BRIEF_TOP_DEFAULT,
    parse_account,
    parse_cutoff,
    parse_window,
    validate_packet_options,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
OBSERVED: Final = datetime(2026, 8, 6, 14, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #


def test_an_account_and_an_output_path_are_both_required() -> None:
    """An export with nowhere to go is not an export."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--account", str(ACCOUNT)])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--output", "snapshot.json"])


def test_the_same_selection_flags_as_the_digest_are_available(tmp_path: Path) -> None:
    """A snapshot should be able to describe exactly what the digest showed."""
    args = cli.build_parser().parse_args(
        [
            "--account",
            str(ACCOUNT),
            "--output",
            str(tmp_path / "s.json"),
            "--as-of",
            "2026-08-06T14:20:00+00:00",
            "--days",
            "3",
            "--symbol",
            "SBIN",
            "--sessions",
            "10",
            "--max-items",
            "7",
            "--no-market",
        ]
    )

    assert (args.days, args.symbol, args.sessions, args.max_items) == (3, ["SBIN"], 10, 7)
    assert args.no_market is True


def test_the_session_default_matches_the_digest_default() -> None:
    """Two commands over one archive must not disagree about a default."""
    exported = cli.build_parser().parse_args(["--account", str(ACCOUNT), "--output", "s.json"])
    shown = digest_cli.build_parser().parse_args(["--account", str(ACCOUNT)])

    assert exported.sessions == shown.sessions == DEFAULT_MULTI_DAY_SESSIONS


def test_the_help_text_states_the_determinism_contract() -> None:
    """Somebody deciding whether to diff two snapshots needs to know the rule."""
    epilog = cli.build_parser().epilog or ""

    assert EXPORT_SCHEMA_VERSION in epilog
    assert "generated_at" in epilog


def test_force_is_off_by_default() -> None:
    """The safe behaviour is the one you get without thinking about it."""
    args = cli.build_parser().parse_args(["--account", str(ACCOUNT), "--output", "s.json"])

    assert args.force is False


# --------------------------------------------------------------------------- #
# Refusing to destroy
# --------------------------------------------------------------------------- #


def test_a_new_path_is_accepted(tmp_path: Path) -> None:
    """The ordinary case writes without ceremony."""
    target = tmp_path / "snapshot.json"

    assert cli._destination(target, force=False) == target


def test_an_existing_file_is_refused_without_force(tmp_path: Path) -> None:
    """A snapshot is the only copy of a fact; it is not replaced by accident."""
    target = tmp_path / "snapshot.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="already exists"):
        cli._destination(target, force=False)


def test_the_refusal_names_the_flag_that_would_allow_it(tmp_path: Path) -> None:
    """Being blocked without being told the way through is just an obstacle."""
    target = tmp_path / "snapshot.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="--force"):
        cli._destination(target, force=False)


def test_an_existing_file_is_replaced_with_force(tmp_path: Path) -> None:
    """Explicit is fine; silent is not."""
    target = tmp_path / "snapshot.json"
    target.write_text("{}", encoding="utf-8")

    assert cli._destination(target, force=True) == target


def test_a_directory_is_refused_even_with_force(tmp_path: Path) -> None:
    """--force permits replacing a snapshot, not removing a directory."""
    target = tmp_path / "somewhere"
    target.mkdir()

    with pytest.raises(ValidationError, match="not a regular file"):
        cli._destination(target, force=True)


def test_a_missing_parent_directory_is_refused(tmp_path: Path) -> None:
    """Failing before the database read is cheaper than failing after it."""
    with pytest.raises(ValidationError, match="does not exist"):
        cli._destination(tmp_path / "nope" / "snapshot.json", force=False)


# --------------------------------------------------------------------------- #
# Shared argument rules
# --------------------------------------------------------------------------- #


def test_the_cutoff_rules_are_the_digest_s_rules() -> None:
    """One implementation, so a snapshot and a digest describe one instant."""
    assert parse_cutoff("2026-08-06T19:50:00+05:30") == OBSERVED
    assert parse_cutoff("2026-08-06T14:20:00") == OBSERVED


def test_an_unparseable_cutoff_is_refused() -> None:
    """A silently defaulted cutoff would export a different question's answer."""
    with pytest.raises(ValidationError, match="ISO-8601"):
        parse_cutoff("whenever")


def test_an_unparseable_account_is_refused() -> None:
    """A message and a status, not a traceback.

    The input has to fail *both* accepted forms. ``not-an-account`` no longer
    does: it is a perfectly good stable label, which is the point of the label
    form and a reminder that "looks wrong to a human" is not the rule.
    """
    with pytest.raises(ValidationError, match="account identifier"):
        parse_account("Not An Account")


def test_a_valid_account_parses() -> None:
    """The identifier that ends up in the snapshot body."""
    assert parse_account(str(ACCOUNT)) == ACCOUNT


@pytest.mark.parametrize("days", [0, -1])
def test_a_non_positive_window_is_refused(days: int) -> None:
    """A window of no days cannot contain an answer."""
    with pytest.raises(ValidationError, match="positive number of days"):
        parse_window(days, fallback=7)


def test_an_omitted_window_falls_back_to_configuration() -> None:
    """One default, defined in the configuration boundary."""
    assert parse_window(None, fallback=9) == timedelta(days=9)


# --------------------------------------------------------------------------- #
# --packet: a compact top-N attention artifact instead of the full snapshot
# --------------------------------------------------------------------------- #


def test_packet_and_top_default_to_off(tmp_path: Path) -> None:
    """The full snapshot is the existing product; a packet is an addition, not a default."""
    parser = cli.build_parser()

    args = parser.parse_args(["--account", str(ACCOUNT), "--output", str(tmp_path / "s.json")])
    assert (args.packet, args.top) == (False, None)

    packet_args = parser.parse_args(
        ["--account", str(ACCOUNT), "--output", str(tmp_path / "p.json"), "--packet", "--top", "3"]
    )
    assert packet_args.packet is True
    assert packet_args.top == 3


def test_packet_help_text_does_not_promise_a_recommendation() -> None:
    """Somebody reading --help must not come away thinking this is a signal."""
    parser = cli.build_parser()
    packet_action = next(
        action for action in parser._actions if "--packet" in action.option_strings
    )

    assert "recommendation" in (packet_action.help or "")


def test_the_help_text_states_both_schema_versions() -> None:
    """An operator deciding whether to diff two files needs to know which schema each uses."""
    epilog = cli.build_parser().epilog or ""

    assert EXPORT_SCHEMA_VERSION in epilog
    assert PACKET_SCHEMA_VERSION in epilog


def test_top_without_packet_is_refused() -> None:
    """--top bounds a packet's own selection; without --packet it would do nothing."""
    with pytest.raises(ValidationError, match="--top is only meaningful together with --packet"):
        validate_packet_options(packet=False, top=3)


def test_packet_alone_or_with_a_valid_top_is_accepted() -> None:
    """The combinations a runbook actually uses must not be refused."""
    validate_packet_options(packet=True, top=None)
    validate_packet_options(packet=True, top=3)
    validate_packet_options(packet=False, top=None)


def test_the_top_default_matches_the_briefs_own_default() -> None:
    """One default, so a packet and a brief agree on what "top" means unqualified."""
    parser = cli.build_parser()
    args = parser.parse_args(["--account", str(ACCOUNT), "--output", "p.json", "--packet"])

    assert args.top is None  # resolved to BRIEF_TOP_DEFAULT inside run(), not by argparse
    assert BRIEF_TOP_DEFAULT == 5
