"""Public API of the C9 ``platform`` context.

This module is the **only** import surface other contexts may use. Importing
``dhruva.contexts.platform.domain``, ``.application``, ``.infrastructure`` or
``.interfaces`` from another context is a build failure.

Re-export here the DTOs, ports and application services that other contexts are
permitted to depend on. Nothing is exported yet -- this context has no
behaviour until its subsystem is built.
"""

from __future__ import annotations

__all__: list[str] = []
