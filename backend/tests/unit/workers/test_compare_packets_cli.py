"""``dhruva-compare-packets`` -- the command surface and its zero-reach guarantee.

What is asserted here: the parser carries no secret-bearing option and no
database/provider/scheduler option; the module imports none of the machinery
that could reach PostgreSQL, Zerodha, or GDELT, and ``run`` is synchronous
throughout (structural proof it cannot await a connection); the filesystem
behaviour -- missing file, non-file, overwrite refusal, ``--force`` -- matches
``dhruva-export``'s own fail-closed rule; and end-to-end ``main()`` exit codes
match this codebase's ``0``/``2`` convention for a clean run versus a refusal.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from dhruva.contexts.intelligence.interfaces import packet_comparison
from dhruva.contexts.intelligence.interfaces.packet_comparison import COMPARISON_SCHEMA_VERSION
from dhruva.contexts.intelligence.interfaces.research_packet import PACKET_SCHEMA_VERSION
from dhruva.shared.errors import ValidationError
from dhruva.workers import compare_packets as cli
from dhruva.workers import digest as digest_cli
from dhruva.workers import export_snapshot as export_cli
from dhruva.workers import refresh as refresh_cli

pytestmark = pytest.mark.unit

#: The vocabulary this slice must never be able to reach -- a database driver,
#: an HTTP client, a broker provider, a news feed, or a scheduler.
_FORBIDDEN_MODULE_FRAGMENTS = (
    "asyncpg",
    "sqlalchemy",
    "psycopg",
    "kiteconnect",
    "zerodha",
    "gdelt",
    "httpx",
    "aiohttp",
    "requests",
    "urllib",
    "socket",
    "celery",
    "schedule",
    "beat",
    "cron",
    "llm",
    "openai",
    "anthropic",
)

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


def _minimal_packet(*, account_id: str = "owner-family", top_n: int = 5) -> dict[str, Any]:
    body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "account_id": account_id,
        "as_of": "2026-08-03T05:35:00+00:00",
        "market_context_requested": True,
        "top_n": top_n,
        "total_ranked": 0,
        "revisions": {"attention": "watchlist-attention-v1"},
        "watchlist_summary": {
            "instruments": 0,
            "with_market_context": 0,
            "with_archived_news": 0,
            "with_attention": 0,
        },
        "attention": [],
    }
    body_json = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    envelope = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "generated_at": "2026-08-07T10:00:00+00:00",
        "body_sha256": hashlib.sha256(body_json.encode("utf-8")).hexdigest(),
    }
    return {"envelope": envelope, "body": body}


def _write_packet(path: Path, *, account_id: str = "owner-family", top_n: int = 5) -> Path:
    packet = _minimal_packet(account_id=account_id, top_n=top_n)
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #


def test_no_option_anywhere_can_carry_a_secret() -> None:
    """No new surface for a secret to leak through, matching every sibling CLI."""
    parser = cli.build_parser()
    names = [option for action in parser._actions for option in action.option_strings]

    assert names
    for name in names:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_IN_OPTIONS:
            assert forbidden not in lowered, f"{name} could carry a secret"


def test_from_and_to_are_both_required() -> None:
    """A comparison with only one side named would compare nothing to nothing."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--to", "b.json"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--from", "a.json"])


def test_from_and_to_parse_to_paths() -> None:
    """The ordinary invocation, with the output and force defaults it should have."""
    args = cli.build_parser().parse_args(["--from", "a.json", "--to", "b.json"])

    assert args.from_path == Path("a.json")
    assert args.to_path == Path("b.json")
    assert args.output is None
    assert args.force is False


def test_no_account_flag_exists() -> None:
    """The account comes from the packets themselves, never from the command line."""
    parser = cli.build_parser()
    names = {option for action in parser._actions for option in action.option_strings}

    assert "--account" not in names


def test_no_daemon_or_scheduler_flag_exists() -> None:
    """Nothing on the command surface starts a loop or a background process."""
    parser = cli.build_parser()
    names = {option for action in parser._actions for option in action.option_strings}

    for forbidden in ("--daemon", "--loop", "--watch", "--interval", "--cron"):
        assert forbidden not in names


def test_the_help_text_states_the_file_to_file_nature() -> None:
    """An operator reading --help must see both schema versions and the no-database promise."""
    epilog = cli.build_parser().epilog or ""
    description = cli.build_parser().description or ""

    assert PACKET_SCHEMA_VERSION in epilog
    assert COMPARISON_SCHEMA_VERSION in epilog
    assert "no database" in description.lower()


# --------------------------------------------------------------------------- #
# No reachable database, provider, or scheduler -- a static proof
# --------------------------------------------------------------------------- #


def _imported_module_roots(module: ModuleType) -> set[str]:
    """Return every top-level module name this module imports, at any depth."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    return roots


@pytest.mark.parametrize("module", [cli, packet_comparison])
def test_no_forbidden_module_is_imported(module: ModuleType) -> None:
    """A static import scan: the whole point is that nothing here ever even names one of these."""
    imports = _imported_module_roots(module)
    for name in imports:
        lowered = name.lower()
        for forbidden in _FORBIDDEN_MODULE_FRAGMENTS:
            assert forbidden not in lowered, f"{name!r} imports forbidden fragment {forbidden!r}"


def test_run_is_synchronous() -> None:
    """A structural guarantee, not just a behavioural one: nothing here awaits."""
    assert not inspect.iscoroutinefunction(cli.run)
    assert not inspect.iscoroutinefunction(cli.main)


def test_run_never_mentions_asyncio() -> None:
    """No asyncio.run() wrapper exists to accidentally add an awaited call to."""
    source = Path(inspect.getfile(cli)).read_text(encoding="utf-8")
    assert "asyncio" not in source


# --------------------------------------------------------------------------- #
# Filesystem behaviour
# --------------------------------------------------------------------------- #


def test_a_missing_input_file_is_refused(tmp_path: Path) -> None:
    """Failing before any JSON parsing is attempted, with a clear diagnostic."""
    missing = tmp_path / "nope.json"
    with pytest.raises(ValidationError, match="does not exist"):
        cli._read_bytes(missing)


def test_a_directory_as_input_is_refused(tmp_path: Path) -> None:
    """A directory is not a packet file, and must not raise an unrelated OSError."""
    directory = tmp_path / "somewhere"
    directory.mkdir()
    with pytest.raises(ValidationError, match="not a regular file"):
        cli._read_bytes(directory)


def test_a_new_output_path_is_accepted(tmp_path: Path) -> None:
    """The ordinary case writes without ceremony."""
    target = tmp_path / "comparison.json"
    assert cli._resolve_output_path(target, force=False) == target


def test_an_existing_output_file_is_refused_without_force(tmp_path: Path) -> None:
    """A saved comparison is not replaced by accident, matching dhruva-export's own rule."""
    target = tmp_path / "comparison.json"
    target.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="already exists"):
        cli._resolve_output_path(target, force=False)


def test_an_existing_output_file_is_replaced_with_force(tmp_path: Path) -> None:
    """Explicit is fine; silent is not."""
    target = tmp_path / "comparison.json"
    target.write_text("{}", encoding="utf-8")

    assert cli._resolve_output_path(target, force=True) == target


def test_a_missing_output_parent_directory_is_refused(tmp_path: Path) -> None:
    """Failing before the comparison runs is cheaper than failing after it."""
    with pytest.raises(ValidationError, match="does not exist"):
        cli._resolve_output_path(tmp_path / "nope" / "c.json", force=False)


# --------------------------------------------------------------------------- #
# End to end through main()
# --------------------------------------------------------------------------- #


def test_main_exits_zero_and_prints_no_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full read -> validate -> compare -> render path, for two identical packets."""
    a = _write_packet(tmp_path / "a.json")
    b = _write_packet(tmp_path / "b.json")

    exit_code = cli.main(["--from", str(a), "--to", str(b)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "No changes between these two packets." in out


def test_main_exits_two_on_a_missing_file(tmp_path: Path) -> None:
    """The refusal exit code this codebase's CLIs use for a rejected input."""
    b = _write_packet(tmp_path / "b.json")

    exit_code = cli.main(["--from", str(tmp_path / "nope.json"), "--to", str(b)])

    assert exit_code == 2


def test_main_exits_two_on_mismatched_accounts(tmp_path: Path) -> None:
    """A cross-account comparison is refused end to end, not just at the validator."""
    a = _write_packet(tmp_path / "a.json", account_id="owner-family")
    b = _write_packet(tmp_path / "b.json", account_id="owner-other")

    exit_code = cli.main(["--from", str(a), "--to", str(b)])

    assert exit_code == 2


def test_main_with_output_writes_a_comparison_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--output writes the canonical JSON comparison, distinct from the packet schema."""
    a = _write_packet(tmp_path / "a.json")
    b = _write_packet(tmp_path / "b.json")
    destination = tmp_path / "out.json"

    exit_code = cli.main(["--from", str(a), "--to", str(b), "--output", str(destination)])

    assert exit_code == 0
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["envelope"]["schema_version"] == COMPARISON_SCHEMA_VERSION
    assert f"wrote {destination}" in capsys.readouterr().out


def test_main_refuses_to_overwrite_output_without_force(tmp_path: Path) -> None:
    """A saved comparison survives an accidental repeat invocation."""
    a = _write_packet(tmp_path / "a.json")
    b = _write_packet(tmp_path / "b.json")
    destination = tmp_path / "out.json"
    destination.write_text("{}", encoding="utf-8")

    exit_code = cli.main(["--from", str(a), "--to", str(b), "--output", str(destination)])

    assert exit_code == 2
    assert destination.read_text(encoding="utf-8") == "{}"


def test_main_overwrites_output_with_force(tmp_path: Path) -> None:
    """--force is the explicit, intentional way past the overwrite refusal."""
    a = _write_packet(tmp_path / "a.json")
    b = _write_packet(tmp_path / "b.json")
    destination = tmp_path / "out.json"
    destination.write_text("{}", encoding="utf-8")

    exit_code = cli.main(
        ["--from", str(a), "--to", str(b), "--output", str(destination), "--force"]
    )

    assert exit_code == 0
    assert destination.read_text(encoding="utf-8") != "{}"


# --------------------------------------------------------------------------- #
# Sibling CLIs remain unchanged (regression smoke checks)
# --------------------------------------------------------------------------- #


def test_sibling_parsers_still_build() -> None:
    """A defensive smoke check: importing this new module must not disturb the others."""
    assert digest_cli.build_parser() is not None
    assert export_cli.build_parser() is not None
    assert refresh_cli.build_parser() is not None
