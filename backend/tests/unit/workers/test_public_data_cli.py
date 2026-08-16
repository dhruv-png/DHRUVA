"""The public reconstruction CLI remains local, explicit, and deterministic."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from dhruva.workers.public_data import build_parser, run


def test_parser_exposes_manual_drop_workflow_without_downloader() -> None:
    """Expose local workflow commands and deliberately omit a downloader."""
    parser = build_parser()
    commands = (
        ("sources",),
        ("inspect-drop", "--drop", "."),
        ("preflight", "--drop", "."),
        (
            "build-manifest",
            "--drop",
            ".",
            "--output",
            ".",
            "--retrieved-at",
            "2026-08-16T12:00:00Z",
        ),
        (
            "reconstruct-universe",
            "--drop",
            ".",
            "--output",
            ".",
            "--retrieved-at",
            "2026-08-16T12:00:00Z",
        ),
        ("coverage", "--drop", "."),
        ("bias", "--drop", "."),
        ("plan", "--source", "nse", "--from", "2021-01-01", "--to", "2026-01-01"),
        ("import", "--manifest", "manifest.json", "--account", "owner-family"),
    )
    assert all(parser.parse_args(command).command == command[0] for command in commands)
    with pytest.raises(SystemExit):
        parser.parse_args(("download",))


@pytest.mark.asyncio
async def test_plan_performs_no_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Planning does not make a network connection."""

    def refused(*_args: object, **_kwargs: object) -> socket.socket:
        raise AssertionError("offline command attempted network access")

    monkeypatch.setattr(socket, "create_connection", refused)
    code = await run(["plan", "--source", "nse", "--from", "2021-01-01", "--to", "2026-01-01"])
    assert code == 0
    assert '"automation_status":"AUTOMATION_UNCLEAR"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_import_refuses_without_explicit_apply(tmp_path: Path) -> None:
    """Refuse database mutation unless the owner supplies the apply flag."""
    assert (
        await run(
            [
                "import",
                "--manifest",
                str(tmp_path / "missing.json"),
                "--account",
                "00000000-0000-0000-0000-000000000001",
            ]
        )
        == 2
    )
