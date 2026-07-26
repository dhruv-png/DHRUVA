"""The architectural controls work as command-line tools.

CI and pre-commit invoke these through their console scripts, so the exit codes
and the messages are the actual contract. A checker that finds a violation but
exits zero would be worse than no checker at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dhruva.tooling import adr_guard, boundaries

VALID_ADR = """# ADR-001 — Example decision

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

Something had to be decided.

## Decision

This was decided.

## Rationale

Because the alternatives were worse.

## Consequences

Some things became easier and some became harder.
"""


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Create a minimal repository with a root marker, source tree and ADR dir."""
    (tmp_path / boundaries.ROOT_MARKER).write_text("marker\n", encoding="utf-8")
    (tmp_path / "backend" / "src" / "dhruva").mkdir(parents=True)
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    return tmp_path


def _module(repo: Path, dotted: str, body: str) -> None:
    path = repo.joinpath("backend", "src", *dotted.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# dhruva-check-boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_boundaries_cli_exits_zero_on_a_clean_tree(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A conforming tree passes silently apart from a confirmation line."""
    monkeypatch.chdir(fake_repo)
    _module(fake_repo, "dhruva.contexts.analytics.domain.regime", "import statistics\n")

    assert boundaries.main([]) == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.unit
def test_boundaries_cli_exits_one_and_names_the_offender(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A breach must fail the build and say exactly where."""
    monkeypatch.chdir(fake_repo)
    _module(
        fake_repo,
        "dhruva.contexts.reference.domain.thing",
        "from dhruva.contexts.trading.domain.order import Order\n",
    )

    assert boundaries.main([]) == 1
    output = capsys.readouterr().out
    assert "violation" in output
    assert "dhruva/contexts/reference/domain/thing.py:1" in output
    assert "[R1]" in output


@pytest.mark.unit
def test_boundaries_cli_accepts_an_explicit_source_root(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CI passes a path when the layout differs from the default."""
    monkeypatch.chdir(fake_repo)
    _module(fake_repo, "dhruva.shared.money", "from decimal import Decimal\n")

    assert boundaries.main(["--source-root", str(fake_repo / "backend" / "src")]) == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.unit
def test_relative_imports_are_still_resolved(fake_repo: Path) -> None:
    """Ruff bans relative imports, but the checker must not be blind to them.

    A rule that can be evaded by changing import style is not a rule.
    """
    _module(
        fake_repo,
        "dhruva.contexts.reference.domain.thing",
        "from ...trading.api import Order\n",
    )
    violations = boundaries.check_tree(fake_repo / "backend" / "src")
    assert any(v.rule == "R2" for v in violations)


@pytest.mark.unit
def test_star_imports_are_checked(fake_repo: Path) -> None:
    """``from x import *`` hides the imported names but not the module."""
    _module(
        fake_repo,
        "dhruva.contexts.reference.domain.thing",
        "from dhruva.contexts.trading.domain.order import *\n",
    )
    violations = boundaries.check_tree(fake_repo / "backend" / "src")
    assert {v.rule for v in violations} == {"R1", "R2"}


@pytest.mark.unit
def test_an_overdeep_relative_import_does_not_crash_the_checker(fake_repo: Path) -> None:
    """More dots than package depth is invalid Python, but must not raise here.

    The checker runs before the interpreter ever sees the file, so it has to
    survive input that will not import. It degrades to treating the tail as
    absolute rather than producing a traceback that masks every other finding.
    """
    _module(fake_repo, "dhruva.shared.money", "from ......dhruva.contexts.risk.api import Limit\n")
    assert boundaries.check_tree(fake_repo / "backend" / "src") != []


@pytest.mark.unit
def test_importing_a_bare_context_package_is_only_a_matrix_question(fake_repo: Path) -> None:
    """``import dhruva.contexts.trading`` exposes no internals, so R1 does not apply.

    R2 still does: the dependency itself must be declared.
    """
    _module(fake_repo, "dhruva.contexts.reference.domain.thing", "import dhruva.contexts.trading\n")
    violations = boundaries.check_tree(fake_repo / "backend" / "src")
    assert {v.rule for v in violations} == {"R2"}


# --------------------------------------------------------------------------- #
# dhruva-adr-guard
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_adr_cli_exits_zero_on_an_intact_log(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The happy path, through the real entry point."""
    monkeypatch.chdir(fake_repo)
    adr = fake_repo / "docs" / "adr"
    (adr / "ADR-001-example-decision.md").write_text(VALID_ADR, encoding="utf-8")

    assert adr_guard.main(["--update"]) == 0
    assert adr_guard.main([]) == 0
    assert "intact" in capsys.readouterr().out


@pytest.mark.unit
def test_adr_cli_reports_nothing_to_register_when_current(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running ``--update`` twice must be a no-op, not a churn."""
    monkeypatch.chdir(fake_repo)
    (fake_repo / "docs" / "adr" / "ADR-001-example-decision.md").write_text(
        VALID_ADR, encoding="utf-8"
    )
    adr_guard.main(["--update"])
    capsys.readouterr()

    assert adr_guard.main(["--update"]) == 0
    assert "nothing to register" in capsys.readouterr().out


@pytest.mark.unit
def test_adr_cli_exits_one_on_a_silent_edit(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control that ADR-027 promises, exercised end to end."""
    monkeypatch.chdir(fake_repo)
    record = fake_repo / "docs" / "adr" / "ADR-001-example-decision.md"
    record.write_text(VALID_ADR, encoding="utf-8")
    adr_guard.main(["--update"])
    record.write_text(VALID_ADR.replace("This was decided.", "Actually, that."), encoding="utf-8")
    capsys.readouterr()

    assert adr_guard.main([]) == 1
    output = capsys.readouterr().out
    assert "problem" in output
    assert "supersedes" in output


@pytest.mark.unit
def test_adr_cli_refuses_an_update_that_would_overwrite(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--update`` must not become a way to launder an edit."""
    monkeypatch.chdir(fake_repo)
    record = fake_repo / "docs" / "adr" / "ADR-001-example-decision.md"
    record.write_text(VALID_ADR, encoding="utf-8")
    adr_guard.main(["--update"])
    record.write_text(VALID_ADR.replace("This was decided.", "Actually, that."), encoding="utf-8")
    capsys.readouterr()

    assert adr_guard.main(["--update"]) == 1
    assert "refused" in capsys.readouterr().out


@pytest.mark.unit
def test_adr_cli_accepts_an_explicit_directory(
    fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Explicit paths keep the tool usable outside a repository checkout."""
    adr = fake_repo / "docs" / "adr"
    (adr / "ADR-001-example-decision.md").write_text(VALID_ADR, encoding="utf-8")

    assert adr_guard.main(["--adr-dir", str(adr), "--update"]) == 0
    assert adr_guard.main(["--adr-dir", str(adr)]) == 0
    assert "intact" in capsys.readouterr().out
