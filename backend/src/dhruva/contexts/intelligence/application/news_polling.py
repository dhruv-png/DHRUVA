"""Poll configured news feeds and hand what they returned to ingestion.

One step above the adapters and one below any scheduler. It fetches each feed,
records what each poll actually was, and forwards only the items a *healthy*
poll produced. There is no background job here: something else decides when this
runs, and until that exists the owner runs it by hand.

A feed that failed does not silently contribute nothing. Its status comes back
in the result so a caller -- a dashboard, a future scheduler, or a person -- can
tell "no news today" from "this source stopped answering three weeks ago".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from dhruva.contexts.intelligence.application.news_ingestion import (
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.domain.sources import SourceHealth

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.contexts.intelligence.domain.news import NewsItem
    from dhruva.contexts.intelligence.domain.ports import IntelligenceUnitOfWork
    from dhruva.contexts.intelligence.domain.sources import NewsFetchResult, SourceStatus
    from dhruva.shared.identity import AccountId

__all__ = ["NewsFeed", "PollNewsFeeds", "PollNewsFeedsCommand", "PollNewsFeedsResult"]


@runtime_checkable
class NewsFeed(Protocol):
    """One configured source that can be polled for provider-neutral items."""

    async def fetch(self) -> NewsFetchResult:
        """Poll once and report both the items and the health of the attempt."""
        ...


@dataclass(frozen=True, slots=True)
class PollNewsFeedsCommand:
    """One polling pass across every configured feed."""

    account_id: AccountId
    universe: tuple[LinkableInstrument, ...]
    analysed_at: datetime


@dataclass(frozen=True, slots=True)
class PollNewsFeedsResult:
    """What each feed reported, and what ingestion did with the usable part."""

    statuses: tuple[tuple[str, SourceStatus], ...]
    items_offered: int
    revisions_added: int
    revisions_unchanged: int
    duplicates: int

    @property
    def unhealthy(self) -> tuple[tuple[str, SourceStatus], ...]:
        """Return every feed whose poll did not reach a usable answer."""
        return tuple((key, status) for key, status in self.statuses if not status.succeeded)


class PollNewsFeeds:
    """Fetch every feed, then ingest what the healthy ones returned."""

    __slots__ = ("_feeds", "_ingest")

    def __init__(
        self,
        feeds: Sequence[NewsFeed],
        unit_of_work_factory: Callable[[AccountId], IntelligenceUnitOfWork],
    ) -> None:
        """Bind the configured feeds and the transaction factory ingestion uses."""
        self._feeds = tuple(feeds)
        self._ingest = IngestNewsItems(unit_of_work_factory)

    async def execute(self, command: PollNewsFeedsCommand) -> PollNewsFeedsResult:
        """Poll each feed once and append everything healthy in one pass.

        Ingestion runs once over the union rather than once per feed, so an item
        syndicated across two sources is deduplicated against the other feed's
        items in the same batch rather than a poll later.
        """
        statuses: list[tuple[str, SourceStatus]] = []
        offered: list[NewsItem] = []
        for feed in self._feeds:
            result = await feed.fetch()
            statuses.append((result.source.key, result.status))
            if result.status.health is SourceHealth.HEALTHY:
                offered.extend(result.usable_items)

        if not offered:
            return PollNewsFeedsResult(
                statuses=tuple(statuses),
                items_offered=0,
                revisions_added=0,
                revisions_unchanged=0,
                duplicates=0,
            )

        ingested = await self._ingest.execute(
            IngestNewsItemsCommand(
                account_id=command.account_id,
                items=tuple(offered),
                universe=command.universe,
                analysed_at=command.analysed_at,
            )
        )
        return PollNewsFeedsResult(
            statuses=tuple(statuses),
            items_offered=len(offered),
            revisions_added=ingested.revisions_added,
            revisions_unchanged=ingested.revisions_unchanged,
            duplicates=ingested.duplicates,
        )
