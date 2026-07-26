"""The quality gates declared in the plan are actually configured.

Configuration drifts silently. Someone under deadline pressure relaxes
``strict`` to unblock a merge, and eighteen months of typing discipline quietly
stops being enforced. These tests make the gates themselves part of the test
suite, so relaxing one is a visible, deliberate act that fails the build.

They assert on Master Project Plan sections 10.1 (Definition of Done),
13.3 (toolchain) and ADR-024 (strict typing).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from dhruva.tooling.boundaries import ALLOWED_CONTEXT_DEPENDENCIES


@pytest.fixture(scope="module")
def pyproject(repo_root: Path) -> dict[str, Any]:
    """Parse ``backend/pyproject.toml``."""
    data: dict[str, Any] = tomllib.loads(
        (repo_root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.unit
def test_python_312_is_required(pyproject: dict[str, Any]) -> None:
    """ADR-024 fixes the language version; older runtimes lack the typing features."""
    assert pyproject["project"]["requires-python"] == ">=3.12"


@pytest.mark.unit
def test_mypy_is_strict(pyproject: dict[str, Any]) -> None:
    """ADR-024: no gradual-typing escape hatch."""
    mypy = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["python_version"] == "3.12"
    assert mypy["warn_unreachable"] is True


@pytest.mark.unit
def test_bare_type_ignore_is_an_error(pyproject: dict[str, Any]) -> None:
    """ADR-024 requires every ignore to name its error code."""
    assert "ignore-without-code" in pyproject["tool"]["mypy"]["enable_error_code"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ("DTZ", "ADR-006: naive datetimes are banned"),
        ("FIX", "Golden Rules: no TODO or FIXME may survive a merge"),
        ("S", "security linting is on by default (ADR-020)"),
        ("ERA", "commented-out code is deleted, not archived in place"),
        ("D", "docstrings are a Definition of Done requirement"),
        ("T20", "print statements are not structured logging"),
        ("ASYNC", "ADR-024: no blocking calls on the event loop"),
    ],
)
def test_required_ruff_rule_families_are_selected(
    pyproject: dict[str, Any], rule: str, why: str
) -> None:
    """Each selected family maps to a specific decision in the plan."""
    assert rule in pyproject["tool"]["ruff"]["lint"]["select"], why


@pytest.mark.unit
def test_relative_imports_are_banned(pyproject: dict[str, Any]) -> None:
    """Absolute imports keep the boundary checker's analysis exact."""
    tidy = pyproject["tool"]["ruff"]["lint"]["flake8-tidy-imports"]
    assert tidy["ban-relative-imports"] == "all"


@pytest.mark.unit
def test_coverage_gate_matches_the_definition_of_done(pyproject: dict[str, Any]) -> None:
    """Plan section 10.1 sets 90% with branch coverage on."""
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 90
    assert pyproject["tool"]["coverage"]["run"]["branch"] is True


@pytest.mark.unit
def test_pytest_is_configured_to_fail_loudly(pyproject: dict[str, Any]) -> None:
    """Warnings become errors and unknown markers are rejected.

    A deprecation warning ignored today is a broken build on an upgrade later.
    """
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in options["addopts"]
    assert "--strict-config" in options["addopts"]
    assert options["xfail_strict"] is True
    assert options["filterwarnings"] == ["error"]


@pytest.mark.unit
def test_declared_markers_cover_the_test_pyramid(pyproject: dict[str, Any]) -> None:
    """Plan section 14.1 names the layers; the markers must match."""
    declared = {m.split(":", 1)[0] for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]}
    assert {"unit", "integration", "contract", "e2e", "slow"} <= declared


@pytest.mark.unit
def test_runtime_dependencies_are_still_empty(pyproject: dict[str, Any]) -> None:
    """S01 is a skeleton.

    A runtime dependency appearing here means a subsystem was started without a
    design document justifying it. When S02 lands, this test is updated in the
    same commit that adds the dependency -- deliberately, not incidentally.
    """
    assert pyproject["project"]["dependencies"] == []


@pytest.mark.unit
def test_console_scripts_expose_the_architectural_controls(pyproject: dict[str, Any]) -> None:
    """Pre-commit and CI both invoke these by name."""
    scripts = pyproject["project"]["scripts"]
    assert scripts["dhruva-check-boundaries"] == "dhruva.tooling.boundaries:main"
    assert scripts["dhruva-adr-guard"] == "dhruva.tooling.adr_guard:main"


@pytest.mark.unit
def test_import_linter_covers_every_context(repo_root: Path) -> None:
    """A context missing from the layer contract would be silently unprotected."""
    config = (repo_root / "backend" / ".importlinter").read_text(encoding="utf-8")
    for context in ALLOWED_CONTEXT_DEPENDENCIES:
        assert f"dhruva.contexts.{context}" in config, f"'{context}' absent from .importlinter"


@pytest.mark.unit
def test_precommit_runs_the_same_gates_as_ci(repo_root: Path) -> None:
    """Local and CI enforcement must not diverge, or CI becomes a surprise."""
    precommit = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for gate in ("ruff", "mypy", "lint-imports", "dhruva-check-boundaries", "dhruva-adr-guard"):
        assert gate in precommit, f"'{gate}' missing from pre-commit"
        assert gate in ci, f"'{gate}' missing from CI"
