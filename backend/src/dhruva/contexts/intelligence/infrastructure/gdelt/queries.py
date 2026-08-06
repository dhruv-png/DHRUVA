"""Turn provider-neutral search batches into documented DOC 2.0 expressions.

Pure, and separate from both the planner above it and the transport beside it.
The planner decides *what to look for* without knowing GDELT exists; this module
knows only how GDELT spells a phrase search, and holds no policy about which
phrases are worth asking for.

DOC 2.0 quotes a multi-word phrase and joins alternatives with ``OR`` inside
parentheses. Nothing here escapes anything: a phrase carrying a quote or a
bracket is refused by the planner before it arrives, because per-provider
escaping rules are exactly the kind of quiet difference that turns one bad
character into a month of wrong results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.search import MAX_BATCH_SIZE
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import GdeltQuery
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.contexts.intelligence.domain.search import SearchBatch, SearchPlan

__all__ = ["gdelt_expression", "gdelt_queries_for", "gdelt_query_for"]

_RESERVED_CHARACTERS = ('"', "(", ")")


def gdelt_expression(texts: Sequence[str]) -> str:
    """Return the DOC 2.0 expression matching any one of ``texts``.

    A single phrase is quoted on its own; several are joined with ``OR`` inside
    parentheses, which is the documented grouping.

    Raises
    ------
    ValidationError
        If the batch is empty, oversized, or carries a phrase that would have to
        be escaped.
    """
    if not texts:
        raise ValidationError("a GDELT query must ask for at least one phrase")
    if len(texts) > MAX_BATCH_SIZE:
        raise ValidationError(
            "a GDELT query batch is larger than the reviewable maximum",
            phrases=len(texts),
            maximum=MAX_BATCH_SIZE,
        )
    for text in texts:
        if not text.strip():
            raise ValidationError("a GDELT query phrase must not be empty")
        if any(character in text for character in _RESERVED_CHARACTERS):
            raise ValidationError(
                "a GDELT query phrase must not contain query operators",
                phrase=text,
            )

    quoted = tuple(f'"{text}"' for text in texts)
    if len(quoted) == 1:
        return quoted[0]
    return "(" + " OR ".join(quoted) + ")"


def gdelt_query_for(
    batch: SearchBatch,
    *,
    timespan: str,
    max_records: int,
) -> GdeltQuery:
    """Return the documented query for one planned batch."""
    return GdeltQuery(
        query=gdelt_expression(batch.texts),
        timespan=timespan,
        max_records=max_records,
    )


def gdelt_queries_for(
    plan: SearchPlan,
    *,
    timespan: str,
    max_records: int,
) -> tuple[GdeltQuery, ...]:
    """Return one query per planned batch, in the plan's order."""
    return tuple(
        gdelt_query_for(batch, timespan=timespan, max_records=max_records) for batch in plan.batches
    )
