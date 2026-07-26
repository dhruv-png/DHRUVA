"""Repository engineering tooling.

Executable enforcement of the architectural rules in Master Project Plan.
These are controls, not conventions: they run in ``pre-commit`` and in CI, and a
breach fails the build.

* :mod:`dhruva.tooling.boundaries` -- bounded-context import rules (section 5).
* :mod:`dhruva.tooling.adr_guard` -- ADR immutability and integrity (ADR-027).

This package is a leaf: it imports nothing from :mod:`dhruva.contexts`.
"""
