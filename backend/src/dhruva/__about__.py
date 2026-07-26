"""Single source of truth for the distribution version.

Read by ``pyproject.toml`` (dynamic version) and by the ``/health`` endpoint, so
the running process can always report exactly which build it is.
"""

from __future__ import annotations

__version__ = "0.3.0"
"""Semantic version.

Minor bumps on subsystem completion, major bumps on gate completion
(Master Project Plan section 13.4).
"""
