"""Poll configured news feeds and hand what they returned to ingestion.

One step above the adapters and one below any scheduler. It fetches each feed,
records what each poll actually was, and forwards only the items a *healthy*
poll produced. There is no background job here: something else decides when this
runs, and until that exists the owner runs it by hand.

A feed that failed does not silently contribute nothing. Its status comes back
in the result so a caller -- a dashboard, a future scheduler, or a person -- can
tell "no news today" from "this source stopped answering three weeks ago".

Being rate-limited ends the pass. A source that has just asked us to slow down
will not be persuaded by the next five requests, and issuing them anyway is the
behaviour a provider blocks rather than throttles. Everything the healthy feeds
already returned is still ingested -- the correct response to "slow down" is to
keep what arrived, not to throw it away -- and the feeds that were never tried
come back marked as never tried rather than as empty.
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

__all__ = [
    "BatchOutcome",
    "NewsFeed",
    "PollNewsFeeds",
    "PollNewsFeedsCommand",
    "PollNewsFeedsResult",
]


@runtime_checkable
class NewsFeed(Protocol):
    """One configured source that can be polled for provider-neutral items."""

    async def fetch(self) -> NewsFetchResult:
        """Poll once and report both the items and the health of the attempt."""
        ...


@dataclass(frozen=True, slots=True)
class PollNewsFeedsCommand:
    """One polling pass across every configured feed.

    ``analysed_at`` is a floor, not the final ingestion cutoff. It is
    typically captured before any feed is fetched -- often before a search
    plan is even built -- so that it can serve as the ``known_at`` a caller
    reads the watchlist at. :meth:`PollNewsFeeds.execute` never ingests
    against it directly: each feed stamps its own items with its own
    retrieval instant, taken after this value was captured, and a live fetch
    routinely takes long enough that those instants land after it. The actual
    cutoff ingestion uses is derived after fetching, from what was actually
    observed, so it can never be earlier than the field on this command.
    """

    account_id: AccountId
    universe: tuple[LinkableInstrument, ...]
    analysed_at: datetime


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """What happened to one configured feed, including never being tried.

    ``status`` is ``None`` for exactly one reason: the pass stopped before this
    feed was reached. That is deliberately not an eighth :class:`SourceHealth`
    value -- health describes what a poll observed, and this batch produced no
    observation to describe.
    """

    index: int
    source_key: str | None
    status: SourceStatus | None
    items_offered: int

    @property
    def attempted(self) -> bool:
        """Return whether this feed was actually polled."""
        return self.status is not None


@dataclass(frozen=True, slots=True)
class PollNewsFeedsResult:
    """What each feed reported, and what ingestion did with the usable part."""

    outcomes: tuple[BatchOutcome, ...]
    items_offered: int
    revisions_added: int
    revisions_unchanged: int
    duplicates: int
    analyses_added: int = 0
    links_added: int = 0
    unresolved: int = 0

    @property
    def statuses(self) -> tuple[tuple[str, SourceStatus], ...]:
        """Return the source key and outcome of every feed that was polled."""
        return tuple(
            (outcome.source_key, outcome.status)
            for outcome in self.outcomes
            if outcome.source_key is not None and outcome.status is not None
        )

    @property
    def unhealthy(self) -> tuple[tuple[str, SourceStatus], ...]:
        """Return every feed whose poll did not reach a usable answer."""
        return tuple((key, status) for key, status in self.statuses if not status.succeeded)

    @property
    def rate_limited(self) -> tuple[BatchOutcome, ...]:
        """Return the batches the source refused for being too frequent."""
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is not None and outcome.status.health is SourceHealth.RATE_LIMITED
        )

    @property
    def skipped(self) -> tuple[BatchOutcome, ...]:
        """Return the batches that were never issued."""
        return tuple(outcome for outcome in self.outcomes if not outcome.attempted)


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

        Polling stops at the first rate-limited feed. Every feed configured after
        it is returned unattempted, and every item collected before it is still
        ingested. Today every feed in a pass is the same provider under a
        different query; when a second provider is configured this rule will need
        to stop only the throttled source, which means a batch will have to
        declare its source before it is polled rather than after.
        """
        outcomes: list[BatchOutcome] = []
        offered: list[NewsItem] = []
        throttled = False
        for index, feed in enumerate(self._feeds, start=1):
            if throttled:
                outcomes.append(
                    BatchOutcome(index=index, source_key=None, status=None, items_offered=0)
                )
                continue
            result = await feed.fetch()
            usable = result.usable_items
            offered.extend(usable)
            outcomes.append(
                BatchOutcome(
                    index=index,
                    source_key=result.source.key,
                    status=result.status,
                    items_offered=len(usable),
                )
            )
            throttled = result.status.health is SourceHealth.RATE_LIMITED

        if not offered:
            return PollNewsFeedsResult(
                outcomes=tuple(outcomes),
                items_offered=0,
                revisions_added=0,
                revisions_unchanged=0,
                duplicates=0,
            )

        # The floor captured before fetching, widened to cover every instant a
        # feed actually stamped. Never earlier than a real observation: it is
        # the later of the caller's own cutoff and the latest genuine
        # first_seen_at this pass produced, not a fresh clock read and not a
        # backdated one. A cutoff taken once before a multi-batch, multi-second
        # live fetch is stale by the time later batches return -- this is not
        # hypothetical, it is what a live poll actually produces.
        analysed_at = max(command.analysed_at, *(item.first_seen_at for item in offered))

        ingested = await self._ingest.execute(
            IngestNewsItemsCommand(
                account_id=command.account_id,
                items=tuple(offered),
                universe=command.universe,
                analysed_at=analysed_at,
            )
        )
        return PollNewsFeedsResult(
            outcomes=tuple(outcomes),
            items_offered=len(offered),
            revisions_added=ingested.revisions_added,
            revisions_unchanged=ingested.revisions_unchanged,
            duplicates=ingested.duplicates,
            analyses_added=ingested.analyses_added,
            links_added=ingested.links_added,
            unresolved=ingested.unresolved,
        )
