"""The distribution exposes a single, well-formed version."""

from __future__ import annotations

import re

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
