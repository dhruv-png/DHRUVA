"""Compose one point-in-time digest of the whole watchlist.

Read-only, and read *once*. The archive is queried a single time over the whole
window with no symbol filter, and the results are grouped in memory by the links
already stored against them. Querying per instrument would issue twenty
statements to answer one question, and — worse — would let two instruments see
different snapshots of a table that is still being appended to.

Nothing is recomputed. The categories, sentiments and instrument links come back
exactly as some earlier ingestion wrote them, which is what makes a digest and
the archive it summarises incapable of disagreeing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.news_ingestion import (
    GetArchivedNews,
    GetArchivedNewsQuery,
)
from dhruva.contexts.intelligence.domain.digest import MAX_ITEMS_PER_INSTRUMENT, build_digest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.digest import WatchlistDigest
    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.contexts.intelligence.domain.ports import IntelligenceUnitOfWork
    from dhruva.shared.identity import AccountId

__all__ = ["BuildWatchlistDigest", "BuildWatchlistDigestQuery"]


@dataclass(frozen=True, slots=True)
class BuildWatchlistDigestQuery:
    """Which instruments, over what window, as known when."""

    account_id: AccountId
    universe: tuple[LinkableInstrument, ...]
    known_at: datetime
    published_from: datetime
    published_to: datetime
    max_items: int = MAX_ITEMS_PER_INSTRUMENT


class BuildWatchlistDigest:
    """Read the archive once and group it under the approved universe."""

    __slots__ = ("_read",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IntelligenceUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._read = GetArchivedNews(unit_of_work_factory)

    async def execute(self, query: BuildWatchlistDigestQuery) -> WatchlistDigest:
        """Return one section per instrument, ordered by what was found.

        The archive read is unfiltered and unbounded on purpose. A ``limit``
        here would truncate the window before grouping, so whichever instruments
        happened to sort last would silently look quiet; the bound belongs on
        what each *section* shows, which is where the domain applies it.
        """
        items = await self._read.execute(
            GetArchivedNewsQuery(
                account_id=query.account_id,
                known_at=query.known_at,
                published_from=query.published_from,
                published_to=query.published_to,
            )
        )
        return build_digest(
            query.universe,
            items,
            known_at=query.known_at,
            published_from=query.published_from,
            published_to=query.published_to,
            max_items=query.max_items,
        )


def instruments_by_symbol(
    universe: Sequence[LinkableInstrument],
    symbols: Sequence[str],
) -> tuple[LinkableInstrument, ...]:
    """Return the requested subset of the universe, preserving universe order.

    An unknown symbol is simply absent from the result. The caller decides
    whether that is an error, because the answer differs: a digest narrowed to
    one instrument should refuse an unknown symbol, while a digest of everything
    has nothing to refuse.
    """
    wanted = {symbol.upper() for symbol in symbols}
    return tuple(entry for entry in universe if entry.canonical_symbol.upper() in wanted)
