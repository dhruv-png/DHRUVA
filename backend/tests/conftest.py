"""Shared fixtures.

Every fixture here resolves paths from the repository root marker rather than
from the current working directory, so the suite behaves identically whether it
is run from ``backend/``, from the repository root, or from CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from dhruva.tooling.boundaries import find_repo_root

# Mutation runs re-execute the suite once per mutant, so the default example
# count would make a run cost hours rather than minutes. The reduced profile is
# selected only by the mutation harness, never by a normal test run.
settings.register_profile(
    "fast", max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile("default", deadline=None)
settings.load_profile("fast" if os.environ.get("DHRUVA_FAST_PROPERTY_TESTS") else "default")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root, located via the ``.dhruva-root`` marker."""
    return find_repo_root(Path(__file__).parent)


@pytest.fixture(scope="session")
def source_root(repo_root: Path) -> Path:
    """Return the directory containing the ``dhruva`` package."""
    return repo_root / "backend" / "src"


@pytest.fixture(scope="session")
def adr_dir(repo_root: Path) -> Path:
    """Return the Architecture Decision Record directory."""
    return repo_root / "docs" / "adr"


@pytest.fixture
def synthetic_source(tmp_path: Path) -> Path:
    """Create an empty stand-in source tree for boundary-checker tests.

    Returns the ``src`` directory, so tests can write modules at
    ``src/dhruva/contexts/<name>/<layer>/<module>.py`` and check them in
    isolation from the real codebase.
    """
    src = tmp_path / "src"
    (src / "dhruva").mkdir(parents=True)
    return src
