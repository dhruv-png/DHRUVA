"""Idempotent, point-in-time news ingestion over provider-neutral items.

The use case takes items an adapter already parsed. It deduplicates them against
what is already stored *and* against each other within the batch, runs the three
deterministic rulesets over the survivors, and appends everything in one
transaction. Running the same poll twice adds nothing and changes no timestamp.

Duplicates are stored, not dropped. A repeat carries the rule that judged it and
the item it repeats, so "why is this headline not in my feed?" has an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.archive import (
    NewsAnalysis,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.entity_linking import link_entities
from dhruva.contexts.intelligence.domain.events import classify_event
from dhruva.contexts.intelligence.domain.news import DeduplicationLedger
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.archive import ArchivedNewsItem
    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.contexts.intelligence.domain.news import NewsItem
    from dhruva.contexts.intelligence.domain.ports import IntelligenceUnitOfWork
    from dhruva.shared.identity import AccountId

__all__ = [
    "GetArchivedNews",
    "GetArchivedNewsQuery",
    "IngestNewsItems",
    "IngestNewsItemsCommand",
    "IngestNewsItemsResult",
]


@dataclass(frozen=True, slots=True)
class IngestNewsItemsCommand:
    """One poll: the items observed, the universe to map them against."""

    account_id: AccountId
    items: tuple[NewsItem, ...]
    universe: tuple[LinkableInstrument, ...]
    analysed_at: datetime


@dataclass(frozen=True, slots=True)
class IngestNewsItemsResult:
    """What one poll actually added, and what it recognised as a repeat."""

    items_observed: int
    duplicates: int
    revisions_added: int
    revisions_unchanged: int
    analyses_added: int
    links_added: int
    unresolved: int


@dataclass(frozen=True, slots=True)
class GetArchivedNewsQuery:
    """Point-in-time parameters for reading the archive.

    ``limit`` bounds what the caller is *shown*, not what the database scans.
    The store returns the whole window and the newest results are kept, because
    pushing the bound into SQL means deciding there which revision of an item
    wins, and that decision belongs with the point-in-time rules rather than in
    a ``LIMIT`` clause. It becomes a repository concern when a window is large
    enough for the difference to be measurable; it is not yet.
    """

    account_id: AccountId
    known_at: datetime
    published_from: datetime
    published_to: datetime
    canonical_symbol: str | None = None
    limit: int | None = None


class IngestNewsItems:
    """Deduplicate, classify and append one poll atomically."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IntelligenceUnitOfWork],
    ) -> None:
        """Bind the use case to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, command: IngestNewsItemsCommand) -> IngestNewsItemsResult:
        """Append every unseen revision and its analysis in one transaction."""
        self._validate_command(command)
        earliest = min(item.published_at for item in command.items)

        async with self._unit_of_work_factory(command.account_id) as unit_of_work:
            ledger = DeduplicationLedger()
            stored = await unit_of_work.news.fingerprints_seen_since(
                published_from=earliest,
                known_at=command.analysed_at,
            )
            ledger.restore(stored)

            staged: list[tuple[NewsRevision, NewsAnalysis | None]] = []
            duplicates = 0
            unresolved = 0
            for item in command.items:
                decision = ledger.observe(item)
                duplicates += int(decision.is_duplicate)
                revision = NewsRevision(
                    item=item,
                    revision=content_revision(item),
                    deduplication=decision,
                )
                # A repeat is recorded with its verdict but not re-analysed: the
                # rulesets already ran on the item it repeats, and running them
                # again would spend work to reach the same answer under a
                # different row.
                analysis = None if decision.is_duplicate else _analyse(item, command)
                if analysis is not None and analysis.mapping.matches == ():
                    unresolved += 1
                staged.append((revision, analysis))

            write = await unit_of_work.news.append(tuple(staged))
            await unit_of_work.commit()

        return IngestNewsItemsResult(
            items_observed=len(command.items),
            duplicates=duplicates,
            revisions_added=write.revisions_added,
            revisions_unchanged=write.revisions_unchanged,
            analyses_added=write.analyses_added,
            links_added=write.links_added,
            unresolved=unresolved,
        )

    @staticmethod
    def _validate_command(command: IngestNewsItemsCommand) -> None:
        """Reject a poll that cannot be stored point-in-time."""
        if not command.items:
            raise ValidationError("a news poll must contain items")
        if command.analysed_at.tzinfo is None or command.analysed_at.utcoffset() is None:
            raise ValidationError("analysed_at must be timezone-aware")
        late = tuple(item for item in command.items if item.first_seen_at > command.analysed_at)
        if late:
            raise ValidationError(
                "an item cannot be observed after the analysis that reads it",
                items=len(late),
            )
        identities = tuple(
            (item.identity.source_key, item.identity.provider_item_id, content_revision(item))
            for item in command.items
        )
        if len(identities) != len(set(identities)):
            raise ValidationError("one poll cannot contain the same item revision twice")


def _analyse(item: NewsItem, command: IngestNewsItemsCommand) -> NewsAnalysis:
    """Run the three deterministic rulesets over one item."""
    text = (
        item.text.title if item.text.snippet is None else f"{item.text.title}. {item.text.snippet}"
    )
    return NewsAnalysis(
        event=classify_event(text),
        sentiment=evaluate_sentiment(text),
        mapping=link_entities(
            text,
            universe=command.universe,
            published_on=item.published_at.date(),
        ),
        analysed_at=command.analysed_at,
    )


class GetArchivedNews:
    """Read the archive as it stood at an explicit knowledge time."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], IntelligenceUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(self, query: GetArchivedNewsQuery) -> tuple[ArchivedNewsItem, ...]:
        """Return the latest revision of each item observable at the cutoff.

        Raises
        ------
        ValidationError
            If the window runs backwards, the cutoff is naive, or the limit is
            not a positive count.
        """
        GetArchivedNews._validate_query(query)
        async with self._unit_of_work_factory(query.account_id) as unit_of_work:
            found = await unit_of_work.news.list_known_at(
                known_at=query.known_at,
                published_from=query.published_from,
                published_to=query.published_to,
                canonical_symbol=query.canonical_symbol,
            )
        return found if query.limit is None else found[: query.limit]

    @staticmethod
    def _validate_query(query: GetArchivedNewsQuery) -> None:
        """Reject a read whose answer could not be interpreted point-in-time."""
        if query.known_at.tzinfo is None or query.known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        if query.published_to < query.published_from:
            raise ValidationError("the published window cannot end before it starts")
        if query.limit is not None and query.limit < 1:
            raise ValidationError("a result limit must be a positive count", limit=query.limit)
