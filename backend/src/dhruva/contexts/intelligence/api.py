"""Public API of the C4 ``intelligence`` context.

This module is the **only** import surface other contexts may use. Importing
``dhruva.contexts.intelligence.domain``, ``.application``, ``.infrastructure`` or
``.interfaces`` from another context is a build failure.

Re-export here the DTOs, ports and application services that other contexts are
permitted to depend on.

Nothing is exported yet, and that is a statement about consumers rather than
about behaviour. The context now holds the news domain -- identity, permitted
text, deduplication, entity linking, event categories and the lexical sentiment
baseline -- but no other context reads it, and a public surface published ahead
of its first consumer is a guess about what that consumer will want.
"""

from __future__ import annotations

__all__: list[str] = []
