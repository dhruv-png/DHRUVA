"""Polling combines feeds, reports their health, and ingests once.

Everything here is in memory: fake feeds, a fake store, a fake transaction. No
scheduler, no network, no HTTP client and no database — polling is orchestration,
and orchestration is exactly the layer that should be testable without any of it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.intelligence.application.news_polling import (
    PollNewsFeeds,
    PollNewsFeedsCommand,
)
from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsArchiveWrite,
    NewsRevision,
)
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.news import (
    NewsFingerprints,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sources import (
    NewsFetchResult,
    SourceHealth,
    SourceStatus,
)
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
RETRIEVED = PUBLISHED + timedelta(minutes=30)
ANALYSED = PUBLISHED + timedelta(hours=1)
ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"

HAL = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
    aliases=("Hindustan Aeronautics",),
)
UNIVERSE = (HAL,)

FEED_A = NewsSource(
    key="feed-a",
    display_name="Feed A",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://feed-a.example",
)
FEED_B = NewsSource(
    key="feed-b",
    display_name="Feed B",
    tier=NewsSourceTier.ESTABLISHED_PUBLISHER,
    homepage_url="https://feed-b.example",
)


def _item(source: NewsSource, url: str, title: str) -> NewsItem:
    link = canonical_url(url)
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=source.key,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=source,
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=RETRIEVED,
    )


def _healthy(source: NewsSource, *items: NewsItem) -> NewsFetchResult:
    return NewsFetchResult(
        source=source,
        status=SourceStatus(
            health=SourceHealth.HEALTHY,
            reason=f"{source.key} returned {len(items)} usable articles",
            observed_at=RETRIEVED,
        ),
        items=items,
        retrieved_at=RETRIEVED,
        content_sha256=hashlib.sha256(source.key.encode()).hexdigest(),
        mapper_revision="fake-feed-v1",
    )


def _unhealthy(source: NewsSource, health: SourceHealth, reason: str) -> NewsFetchResult:
    return NewsFetchResult(
        source=source,
        status=SourceStatus(health=health, reason=reason, observed_at=RETRIEVED),
        items=(),
        retrieved_at=RETRIEVED,
        content_sha256=hashlib.sha256(source.key.encode()).hexdigest(),
        mapper_revision="fake-feed-v1",
    )


class FakeFeed:
    """Return one arranged result and record that it was polled exactly once."""

    def __init__(self, result: NewsFetchResult) -> None:
        self.result = result
        self.polls = 0

    async def fetch(self) -> NewsFetchResult:
        """Return the arranged result."""
        self.polls += 1
        return self.result


class FakeNewsStore:
    """An in-memory archive reproducing the real append and lookup semantics."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[NewsRevision, NewsAnalysis | None]] = {}

    async def append(
        self,
        revisions: tuple[tuple[NewsRevision, NewsAnalysis | None], ...],
    ) -> NewsArchiveWrite:
        """Insert unseen rows and report identical retries."""
        added = 0
        analyses = 0
        links = 0
        for revision, analysis in revisions:
            key = (
                revision.item.identity.source_key,
                revision.item.identity.provider_item_id,
                revision.revision,
            )
            if key in self.rows:
                continue
            self.rows[key] = (revision, analysis)
            added += 1
            if analysis is not None:
                analyses += 1
                links += len(analysis.linked)
        return NewsArchiveWrite(
            revisions_added=added,
            revisions_unchanged=len(revisions) - added,
            analyses_added=analyses,
            links_added=links,
        )

    async def list_known_at(
        self,
        *,
        known_at: datetime,
        published_from: datetime,
        published_to: datetime,
        canonical_symbol: str | None = None,
    ) -> tuple[ArchivedNewsItem, ...]:
        """Fail because polling must never read the archive back."""
        raise AssertionError((known_at, published_from, published_to, canonical_symbol))

    async def fingerprints_seen_since(
        self,
        *,
        published_from: datetime,
        known_at: datetime,
    ) -> tuple[tuple[NewsItemIdentity, NewsFingerprints], ...]:
        """Return the fingerprints of non-duplicate rows inside the window."""
        assert published_from <= known_at
        return tuple(
            (revision.item.identity, revision.item.fingerprints)
            for revision, _ in self.rows.values()
            if not revision.deduplication.is_duplicate
        )


class FakeUnitOfWork:
    """Expose one in-memory store and record the transaction outcome."""

    def __init__(self, store: FakeNewsStore) -> None:
        self.news = store
        self.commits = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the fake transaction."""

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Discard nothing; the fake stages directly."""


class Factory:
    """Retain created transactions so 'ingestion ran once' is observable."""

    def __init__(self, store: FakeNewsStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction for the arranged account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _command() -> PollNewsFeedsCommand:
    return PollNewsFeedsCommand(account_id=ACCOUNT, universe=UNIVERSE, analysed_at=ANALYSED)


def _stored_keys(store: FakeNewsStore) -> set[tuple[str, str]]:
    return {(key[0], key[1]) for key in store.rows}


# --------------------------------------------------------------------------- #
# The happy paths
# --------------------------------------------------------------------------- #


async def test_one_healthy_feed_passes_its_items_to_ingestion() -> None:
    """The whole point: what a feed found reaches the archive."""
    store = FakeNewsStore()
    factory = Factory(store)
    feed = FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([feed], factory).execute(_command())

    assert feed.polls == 1
    assert result.items_offered == 1
    assert result.revisions_added == 1
    assert len(store.rows) == 1
    assert factory.created[0].commits == 1


async def test_several_healthy_feeds_are_combined_and_ingested_once() -> None:
    """One transaction over the union, not one per feed."""
    store = FakeNewsStore()
    factory = Factory(store)
    feeds = [
        FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE))),
        FakeFeed(
            _healthy(
                FEED_B,
                _item(FEED_B, "https://feed-b.example/2", "State Bank of India cuts rates"),
                _item(FEED_B, "https://feed-b.example/3", "Bajaj Finance approves a dividend"),
            )
        ),
    ]

    result = await PollNewsFeeds(feeds, factory).execute(_command())

    assert [feed.polls for feed in feeds] == [1, 1]
    assert result.items_offered == 3
    assert result.revisions_added == 3
    assert len(factory.created) == 1


async def test_a_syndicated_item_across_two_feeds_is_deduplicated_in_one_pass() -> None:
    """Ingesting the union is what lets the second copy be caught immediately.

    Polling feed by feed would store both and only notice on the next run, by
    which point the duplicate is already in a reader's list.
    """
    store = FakeNewsStore()
    factory = Factory(store)
    feeds = [
        FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://wire.example/story", ORDER_HEADLINE))),
        FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://wire.example/story", ORDER_HEADLINE))),
    ]

    result = await PollNewsFeeds(feeds, factory).execute(_command())

    assert result.items_offered == 2
    assert result.duplicates == 1
    assert result.revisions_added == 2
    analysed = [analysis for _, analysis in store.rows.values() if analysis is not None]
    assert len(analysed) == 1
    repeats = [
        revision for revision, _ in store.rows.values() if revision.deduplication.is_duplicate
    ]
    assert len(repeats) == 1
    assert repeats[0].deduplication.rule is not None


# --------------------------------------------------------------------------- #
# Health is never flattened
# --------------------------------------------------------------------------- #


async def test_an_empty_successful_feed_is_not_an_operational_failure() -> None:
    """The feed answered and had nothing. That is a fact, not an outage."""
    store = FakeNewsStore()
    factory = Factory(store)
    feed = FakeFeed(_unhealthy(FEED_A, SourceHealth.EMPTY_RESULT, "nothing matched the query"))

    result = await PollNewsFeeds([feed], factory).execute(_command())

    assert result.statuses == (("feed-a", feed.result.status),)
    assert result.statuses[0][1].succeeded
    assert result.unhealthy == ()
    assert result.items_offered == 0
    assert factory.created == []


@pytest.mark.parametrize(
    ("health", "reason"),
    [
        (SourceHealth.TEMPORARILY_UNAVAILABLE, "the feed timed out"),
        (SourceHealth.RATE_LIMITED, "the feed asked for a slower rate"),
        (SourceHealth.AUTHENTICATION_FAILED, "the feed refused the request"),
        (SourceHealth.MALFORMED_PAYLOAD, "the feed returned unparseable bytes"),
        (SourceHealth.STALE, "the feed has stopped moving"),
        (SourceHealth.UNSUPPORTED_SCHEMA, "the feed changed shape"),
    ],
)
async def test_an_unhealthy_feed_is_reported_and_never_becomes_an_empty_success(
    health: SourceHealth,
    reason: str,
) -> None:
    """Every operational failure survives the poll intact and says which it was."""
    store = FakeNewsStore()
    factory = Factory(store)
    feed = FakeFeed(_unhealthy(FEED_A, health, reason))

    result = await PollNewsFeeds([feed], factory).execute(_command())

    assert result.statuses[0][1].health is health
    assert result.statuses[0][1].reason == reason
    assert result.statuses[0][1].succeeded is False
    assert result.unhealthy == (("feed-a", feed.result.status),)
    assert result.items_offered == 0
    assert store.rows == {}


async def test_one_failing_feed_does_not_lose_another_feed_s_items() -> None:
    """A partial outage degrades coverage; it must not discard what did arrive.

    This is the application contract: one poll, best effort per source, with the
    failure reported rather than raised. A caller that needs all-or-nothing can
    read ``unhealthy`` and decide for itself.
    """
    store = FakeNewsStore()
    factory = Factory(store)
    feeds = [
        FakeFeed(_unhealthy(FEED_A, SourceHealth.TEMPORARILY_UNAVAILABLE, "the feed timed out")),
        FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE))),
    ]

    result = await PollNewsFeeds(feeds, factory).execute(_command())

    assert result.revisions_added == 1
    assert len(store.rows) == 1
    assert len(result.unhealthy) == 1
    assert result.unhealthy[0][0] == "feed-a"
    assert len(result.statuses) == 2


async def test_a_non_healthy_result_contributes_no_items_even_if_it_carried_some() -> None:
    """The invariant holds at the type; polling relies on it rather than re-checking.

    ``NewsFetchResult`` refuses to construct a non-healthy result with items, so
    there is no path by which a failed poll can smuggle one into ingestion.
    """
    from dhruva.shared.errors import InvariantViolation  # noqa: PLC0415 - the point of the test

    with pytest.raises(InvariantViolation, match="only a healthy poll carries items"):
        NewsFetchResult(
            source=FEED_A,
            status=SourceStatus(
                health=SourceHealth.STALE,
                reason="stale but carrying items",
                observed_at=RETRIEVED,
            ),
            items=(_item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE),),
            retrieved_at=RETRIEVED,
            content_sha256=hashlib.sha256(b"x").hexdigest(),
            mapper_revision="fake-feed-v1",
        )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


async def test_feed_order_does_not_change_what_is_ingested() -> None:
    """Configuration order is not data; the same sources must ingest the same rows."""
    first_store = FakeNewsStore()
    second_store = FakeNewsStore()
    left = _healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE))
    right = _healthy(
        FEED_B,
        _item(FEED_B, "https://feed-b.example/2", "State Bank of India cuts lending rates"),
    )

    forward = await PollNewsFeeds([FakeFeed(left), FakeFeed(right)], Factory(first_store)).execute(
        _command()
    )
    reversed_order = await PollNewsFeeds(
        [FakeFeed(right), FakeFeed(left)], Factory(second_store)
    ).execute(_command())

    assert _stored_keys(first_store) == _stored_keys(second_store)
    assert forward.items_offered == reversed_order.items_offered
    assert forward.revisions_added == reversed_order.revisions_added
    assert forward.duplicates == reversed_order.duplicates
    assert {key for key, _ in forward.statuses} == {key for key, _ in reversed_order.statuses}


async def test_no_feeds_at_all_opens_no_transaction() -> None:
    """Nothing configured is not an error, and must not touch the database."""
    factory = Factory(FakeNewsStore())

    result = await PollNewsFeeds([], factory).execute(_command())

    assert result.statuses == ()
    assert result.items_offered == 0
    assert factory.created == []


async def test_polling_never_reads_the_archive_back() -> None:
    """A poll appends. Anything that wants to read has its own query."""
    store = FakeNewsStore()
    feed = FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE)))

    await PollNewsFeeds([feed], Factory(store)).execute(_command())

    assert len(store.rows) == 1


# --------------------------------------------------------------------------- #
# Being told to slow down
# --------------------------------------------------------------------------- #


def _rate_limited(source: NewsSource) -> NewsFetchResult:
    return _unhealthy(source, SourceHealth.RATE_LIMITED, "the source asked for a slower rate")


async def test_a_rate_limited_batch_stops_every_later_request() -> None:
    """A source that just said "slow down" is not persuaded by five more tries.

    Continuing is the behaviour a provider blocks rather than throttles, so the
    later feeds are not polled at all.
    """
    store = FakeNewsStore()
    first = FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE)))
    throttled = FakeFeed(_rate_limited(FEED_A))
    later = FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([first, throttled, later], Factory(store)).execute(_command())

    assert first.polls == 1
    assert throttled.polls == 1
    assert later.polls == 0
    assert [outcome.attempted for outcome in result.outcomes] == [True, True, False]


async def test_items_from_before_a_rate_limit_are_still_ingested() -> None:
    """Keeping what arrived is the correct response to being throttled.

    Discarding it would turn a provider's pacing request into data loss, and the
    next pass would have to fetch the same articles again.
    """
    store = FakeNewsStore()
    healthy = FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE)))
    throttled = FakeFeed(_rate_limited(FEED_B))

    result = await PollNewsFeeds([healthy, throttled], Factory(store)).execute(_command())

    assert result.items_offered == 1
    assert result.revisions_added == 1
    assert len(store.rows) == 1


async def test_a_skipped_batch_is_never_reported_as_an_empty_success() -> None:
    """Not asked" and "asked and got nothing" are different facts."""
    throttled = FakeFeed(_rate_limited(FEED_A))
    skipped = FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([throttled, skipped], Factory(FakeNewsStore())).execute(_command())

    assert result.skipped[0].index == 2
    assert result.skipped[0].status is None
    assert result.skipped[0].source_key is None
    assert result.statuses == (("feed-a", throttled.result.status),)


async def test_the_rate_limited_batch_is_identified_by_position() -> None:
    """An operator has to be able to say which request was refused."""
    feeds = [
        FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE))),
        FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/2", ORDER_HEADLINE))),
        FakeFeed(_rate_limited(FEED_A)),
        FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE))),
    ]

    result = await PollNewsFeeds(feeds, Factory(FakeNewsStore())).execute(_command())

    assert [outcome.index for outcome in result.rate_limited] == [3]
    assert [outcome.index for outcome in result.skipped] == [4]


@pytest.mark.parametrize(
    "health",
    [
        SourceHealth.TEMPORARILY_UNAVAILABLE,
        SourceHealth.MALFORMED_PAYLOAD,
        SourceHealth.UNSUPPORTED_SCHEMA,
        SourceHealth.STALE,
        SourceHealth.AUTHENTICATION_FAILED,
    ],
)
async def test_only_rate_limiting_stops_the_pass(health: SourceHealth) -> None:
    """Other failures degrade coverage; they do not mean "stop asking".

    A malformed payload from one query says nothing about the next one, and
    abandoning the pass would turn one bad response into a day with no news.
    """
    failing = FakeFeed(_unhealthy(FEED_A, health, "the feed did not answer usefully"))
    later = FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([failing, later], Factory(FakeNewsStore())).execute(_command())

    assert later.polls == 1
    assert result.skipped == ()
    assert result.items_offered == 1


async def test_an_empty_successful_batch_does_not_stop_the_pass() -> None:
    """Nothing to report is a successful answer, not a reason to give up."""
    empty = FakeFeed(_unhealthy(FEED_A, SourceHealth.EMPTY_RESULT, "no articles matched"))
    later = FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([empty, later], Factory(FakeNewsStore())).execute(_command())

    assert later.polls == 1
    assert result.unhealthy == ()
    assert result.items_offered == 1


async def test_every_batch_is_accounted_for_whatever_happened() -> None:
    """One outcome per configured feed, in the configured order."""
    feeds = [
        FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE))),
        FakeFeed(_rate_limited(FEED_A)),
        FakeFeed(_healthy(FEED_B, _item(FEED_B, "https://feed-b.example/1", ORDER_HEADLINE))),
    ]

    result = await PollNewsFeeds(feeds, Factory(FakeNewsStore())).execute(_command())

    assert [outcome.index for outcome in result.outcomes] == [1, 2, 3]
    assert len(result.outcomes) == len(feeds)


async def test_the_result_carries_the_analysis_counts_ingestion_produced() -> None:
    """An operator reads these; deriving them again would let them disagree."""
    store = FakeNewsStore()
    feed = FakeFeed(_healthy(FEED_A, _item(FEED_A, "https://feed-a.example/1", ORDER_HEADLINE)))

    result = await PollNewsFeeds([feed], Factory(store)).execute(_command())

    assert result.analyses_added == 1
    assert result.links_added == 1
    assert result.unresolved == 0


async def test_an_unmatched_headline_is_counted_as_unresolved() -> None:
    """Ingesting an article about nothing we follow is not a silent success."""
    store = FakeNewsStore()
    feed = FakeFeed(
        _healthy(FEED_A, _item(FEED_A, "https://feed-a.example/9", "Rainfall delays the harvest"))
    )

    result = await PollNewsFeeds([feed], Factory(store)).execute(_command())

    assert result.unresolved == 1
    assert result.links_added == 0
