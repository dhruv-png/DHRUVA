"""Repository-level secret hygiene (ADR-033).

CI runs gitleaks over full history; these tests cover the invariants that are
cheap to assert locally and that fail fastest when broken.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Patterns that look like real credentials rather than placeholders.
_SUSPICIOUS = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


@pytest.mark.unit
def test_no_env_file_is_present_in_the_tree(repo_root: Path) -> None:
    """A committed .env is a leaked credential set, not a convenience."""
    offenders = [
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob(".env*")
        if path.is_file() and path.name != ".env.example" and ".git/" not in path.as_posix()
    ]

    assert offenders == [], f"remove and rotate: {offenders}"


@pytest.mark.unit
def test_gitignore_covers_the_patterns_that_matter(repo_root: Path) -> None:
    """Prevention is cheaper than detection, and both are required."""
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8")

    for pattern in (".env", "*.pem", "*.key", "secrets/"):
        assert pattern in ignored, f"{pattern} is not ignored"


@pytest.mark.unit
def test_the_example_env_file_contains_no_realistic_value(repo_root: Path) -> None:
    """`.env.example` documents variable names; it must not document values."""
    example = (repo_root / "infra" / ".env.example").read_text(encoding="utf-8")

    assert not _SUSPICIOUS.search(example)
    assert "local_only" in example, "placeholders should be obviously fake"


@pytest.mark.unit
def test_no_source_file_contains_a_credential_shaped_string(repo_root: Path) -> None:
    """A crude scan is the right tool here: it catches the earliest version."""
    offenders: list[str] = []
    for path in (repo_root / "backend" / "src").rglob("*.py"):
        if _SUSPICIOUS.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(repo_root).as_posix())

    assert offenders == []


@pytest.mark.unit
def test_ci_scans_full_history_not_just_the_diff(repo_root: Path) -> None:
    """Diff-only scanning gives a green build to a repo that already leaked.

    This is the decision that matters most in ADR-033, so it is asserted.
    """
    workflow = (repo_root / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")

    assert "gitleaks" in workflow
    assert "fetch-depth: 0" in workflow, "full history is required for the scan to be meaningful"


@pytest.mark.unit
def test_dependency_audit_checks_the_hash_pinned_deployment_lock(repo_root: Path) -> None:
    """The release audit targets what production installs, not the editable project."""
    workflow = (repo_root / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    deployment_lock = (repo_root / "backend" / "requirements.lock").read_text(encoding="utf-8")

    assert "pip-audit --strict --desc --require-hashes" in workflow
    assert "--requirement backend/requirements.lock" in workflow
    assert "--hash=sha256:" in deployment_lock
    assert "\ndhruva==" not in deployment_lock, (
        "the local editable distribution is not a third-party deployable dependency"
    )


@pytest.mark.unit
def test_dependency_audit_cannot_suppress_findings_or_its_exit_code(repo_root: Path) -> None:
    """A vulnerable third party must make the security job fail closed."""
    workflow = (repo_root / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")

    assert "--ignore-vuln" not in workflow
    assert "continue-on-error" not in workflow
    assert "|| true" not in workflow
