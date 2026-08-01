"""The distribution exposes a single, well-formed version."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import dhruva
from dhruva.__about__ import __version__

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@pytest.mark.unit
def test_version_is_semver() -> None:
    """Version strings must be parseable; releases are cut from them."""
    assert SEMVER.match(__version__), f"{__version__!r} is not a semantic version"


@pytest.mark.unit
def test_package_reexports_version() -> None:
    """``dhruva.__version__`` is the import path the health endpoint will use."""
    assert dhruva.__version__ == __version__


@pytest.mark.unit
def test_stage_one_version_is_pre_release() -> None:
    """Stage 1 is pre-1.0; 1.0.0 is reserved for the gate that ships live trading."""
    major = int(__version__.split(".", 1)[0])
    assert major == 0, "1.0.0 is reserved for Gate G5 (ADR-026)"


#: A released changelog heading: ``## [0.4.0] — 2026-07-29 — S04 ...``. The
#: ``Unreleased`` section has no version in its brackets and is skipped by the
#: pattern rather than by a special case.
_RELEASED = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


@pytest.mark.unit
def test_the_version_matches_the_newest_released_changelog_entry(repo_root: Path) -> None:
    """The version string and the changelog must name the same release.

    This test exists because they did not. `v0.4.0` was tagged and its changelog
    section written while ``__version__`` still said ``0.3.0`` -- so a deployed
    S04 build reported the previous release at ``/health`` and in its
    distribution metadata, which is the value this module's own docstring says
    those two things read.

    Every gate stayed green throughout, because the existing assertions here ask
    whether the version is *well-formed* rather than whether it is *true*. A
    version nobody compares to anything is a string, and the only thing a string
    can be wrong about is everything.

    The comparison is against the newest **released** section rather than the
    tag, because a working tree has no tag and this must fail in CI on the commit
    that forgets the bump -- not afterwards, on the machine doing the tagging.
    """
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    released = _RELEASED.findall(changelog)

    assert released, "the changelog names no released version"
    assert __version__ == released[0], (
        f"__version__ is {__version__!r} but the newest released changelog entry "
        f"is {released[0]!r}. Bump one, or move the release section, in the same "
        f"commit as the other."
    )
