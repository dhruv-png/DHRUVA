"""The whole pass, end to end, against real PostgreSQL.

The unit suites prove each piece in isolation with fakes. This one proves the
pieces compose: a poll through the real transport contract (with an injected
feed rather than a network), a real transaction, a real schema, and then the
read that answers "what did DHRUVA know at this instant?".

Only the network is faked. Everything below the feed is the code that runs in
production, because the point-in-time rules are the kind that pass against a
dictionary and fail against a ``TIMESTAMPTZ`` (ADR-058).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    GetArchivedNews,
    GetArchivedNewsQuery,
)
from dhruva.contexts.intelligence.application.news_polling import (
    PollNewsFeeds,
    PollNewsFeedsCommand,
)
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.news import (
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sources import (
    NewsFetchResult,
    SourceHealth,
    SourceStatus,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_ATTRIBUTION_URL,
    GDELT_SOURCE_KEY,
    gdelt_source,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    NewsAnalysisModel,
    NewsItemRevisionModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.news_presentation import render_items
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
FIRST_SEEN = PUBLISHED + timedelta(minutes=30)
ANALYSED = FIRST_SEEN + timedelta(minutes=5)
CORRECTED_SEEN = FIRST_SEEN + timedelta(hours=4)
CORRECTED_ANALYSED = CORRECTED_SEEN + timedelta(minutes=5)

ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"
CORRECTED_HEADLINE = "Hindustan Aeronautics bags order worth Rs 4,200 crore from the ministry"
BANK_HEADLINE = "State Bank of India raises deposit rates across tenures"

HAL = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
    aliases=("Hindustan Aeronautics",),
)
SBIN = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-sbin"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
UNIVERSE = (HAL, SBIN)

PUBLISHER = "economictimes.indiatimes.test"


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyIntelligenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


def _item(
    url: str,
    title: str,
    *,
    first_seen_at: datetime = FIRST_SEEN,
    source: NewsSource | None = None,
) -> NewsItem:
    """Build an item the way the GDELT mapper does, including its identity rule."""
    link = canonical_url(url)
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT_SOURCE_KEY,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=source or gdelt_source(PUBLISHER),
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=first_seen_at,
    )


class _Feed:
    """One arranged fetch result, standing in for one GDELT request."""

    def __init__(self, result: NewsFetchResult) -> None:
        self.result = result
        self.polls = 0

    async def fetch(self) -> NewsFetchResult:
        """Return the arranged result and record that it was asked for."""
        self.polls += 1
        return self.result


def _healthy(*items: NewsItem, observed_at: datetime = FIRST_SEEN) -> NewsFetchResult:
    return NewsFetchResult(
        source=gdelt_source(),
        status=SourceStatus(
            health=SourceHealth.HEALTHY,
            reason=f"GDELT returned {len(items)} articles",
            observed_at=observed_at,
        ),
        items=items,
        retrieved_at=observed_at,
        content_sha256=hashlib.sha256(b"payload").hexdigest(),
        mapper_revision="gdelt-doc2-artlist-v1",
    )


def _unhealthy(health: SourceHealth, *, observed_at: datetime = FIRST_SEEN) -> NewsFetchResult:
    return NewsFetchResult(
        source=gdelt_source(),
        status=SourceStatus(
            health=health, reason=f"GDELT reported {health}", observed_at=observed_at
        ),
        items=(),
        retrieved_at=observed_at,
        content_sha256=hashlib.sha256(b"").hexdigest(),
        mapper_revision="gdelt-doc2-artlist-v1",
    )


def _poll_command(analysed_at: datetime = ANALYSED) -> PollNewsFeedsCommand:
    return PollNewsFeedsCommand(account_id=ACCOUNT, universe=UNIVERSE, analysed_at=analysed_at)


def _read(
    known_at: datetime,
    *,
    symbol: str | None = None,
    limit: int | None = None,
) -> GetArchivedNewsQuery:
    return GetArchivedNewsQuery(
        account_id=ACCOUNT,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=2),
        published_to=PUBLISHED + timedelta(days=2),
        canonical_symbol=symbol,
        limit=limit,
    )


async def _count(engine: AsyncEngine, model: type) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


# --------------------------------------------------------------------------- #
# Ingest, then read
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_polled_batch_can_be_read_back_at_a_later_cutoff(
    migrated: AsyncEngine,
) -> None:
    """The round trip the whole slice exists for."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))

    polled = await PollNewsFeeds([feed], factory).execute(_poll_command())
    found = await GetArchivedNews(factory).execute(_read(ANALYSED + timedelta(hours=1)))

    assert polled.revisions_added == 1
    assert len(found) == 1
    assert found[0].revision.item.text.title == ORDER_HEADLINE


@pytest.mark.usefixtures("truncated_after_test")
async def test_several_batches_are_ingested_in_one_pass(migrated: AsyncEngine) -> None:
    """Batching is a request-shaping decision, not a storage one."""
    factory = _factory(migrated)
    feeds = [
        _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE))),
        _Feed(_healthy(_item(f"https://{PUBLISHER}/sbi-rates", BANK_HEADLINE))),
    ]

    polled = await PollNewsFeeds(feeds, factory).execute(_poll_command())

    assert polled.revisions_added == 2
    assert await _count(migrated, NewsItemRevisionModel) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_read_can_be_narrowed_to_one_instrument(migrated: AsyncEngine) -> None:
    """News about SBIN" must not include the aerospace order."""
    factory = _factory(migrated)
    feed = _Feed(
        _healthy(
            _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE),
            _item(f"https://{PUBLISHER}/sbi-rates", BANK_HEADLINE),
        )
    )

    await PollNewsFeeds([feed], factory).execute(_poll_command())
    found = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED, symbol="SBIN"))

    assert [item.revision.item.text.title for item in found] == [BANK_HEADLINE]


@pytest.mark.usefixtures("truncated_after_test")
async def test_repeating_the_pass_stores_nothing_new(migrated: AsyncEngine) -> None:
    """A re-poll is a no-op, through the real unique constraints."""
    factory = _factory(migrated)
    item = _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)

    first = await PollNewsFeeds([_Feed(_healthy(item))], factory).execute(_poll_command())
    second = await PollNewsFeeds([_Feed(_healthy(item))], factory).execute(_poll_command())

    assert first.revisions_added == 1
    assert second.revisions_added == 0
    assert second.revisions_unchanged == 1
    assert await _count(migrated, NewsItemRevisionModel) == 1
    assert await _count(migrated, NewsAnalysisModel) == 1


# --------------------------------------------------------------------------- #
# Point-in-time correctness
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_correction_is_visible_only_after_it_was_first_seen(
    migrated: AsyncEngine,
) -> None:
    """The property a backtest depends on, asserted at both instants.

    Before the correction was observed, the archive must still return the
    original wording. Anything else is hindsight leaking into a replay.
    """
    factory = _factory(migrated)
    original = _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)
    corrected = _item(
        f"https://{PUBLISHER}/hal-order", CORRECTED_HEADLINE, first_seen_at=CORRECTED_SEEN
    )

    await PollNewsFeeds([_Feed(_healthy(original))], factory).execute(_poll_command())
    await PollNewsFeeds([_Feed(_healthy(corrected, observed_at=CORRECTED_SEEN))], factory).execute(
        _poll_command(CORRECTED_ANALYSED)
    )

    before = await GetArchivedNews(factory).execute(_read(ANALYSED + timedelta(minutes=1)))
    after = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED + timedelta(minutes=1)))

    assert [item.revision.item.text.title for item in before] == [ORDER_HEADLINE]
    assert [item.revision.item.text.title for item in after] == [CORRECTED_HEADLINE]


@pytest.mark.usefixtures("truncated_after_test")
async def test_both_revisions_are_stored_rather_than_one_overwriting_the_other(
    migrated: AsyncEngine,
) -> None:
    """There is no update path; a correction appends beside its original."""
    factory = _factory(migrated)
    original = _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)
    corrected = _item(
        f"https://{PUBLISHER}/hal-order", CORRECTED_HEADLINE, first_seen_at=CORRECTED_SEEN
    )

    await PollNewsFeeds([_Feed(_healthy(original))], factory).execute(_poll_command())
    await PollNewsFeeds([_Feed(_healthy(corrected, observed_at=CORRECTED_SEEN))], factory).execute(
        _poll_command(CORRECTED_ANALYSED)
    )

    assert await _count(migrated, NewsItemRevisionModel) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_nothing_first_seen_after_the_cutoff_is_ever_returned(
    migrated: AsyncEngine,
) -> None:
    """The cutoff is a knowledge cutoff, not a publication one."""
    factory = _factory(migrated)
    late = _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE, first_seen_at=CORRECTED_SEEN)

    await PollNewsFeeds([_Feed(_healthy(late, observed_at=CORRECTED_SEEN))], factory).execute(
        _poll_command(CORRECTED_ANALYSED)
    )
    found = await GetArchivedNews(factory).execute(_read(FIRST_SEEN + timedelta(minutes=1)))

    assert found == ()
    assert await _count(migrated, NewsItemRevisionModel) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_read_is_deterministic_when_repeated(migrated: AsyncEngine) -> None:
    """The same question at the same cutoff has to have the same answer."""
    factory = _factory(migrated)
    feed = _Feed(
        _healthy(
            _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE),
            _item(f"https://{PUBLISHER}/sbi-rates", BANK_HEADLINE),
        )
    )
    await PollNewsFeeds([feed], factory).execute(_poll_command())

    first = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))
    second = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))

    assert [item.revision.revision for item in first] == [item.revision.revision for item in second]


# --------------------------------------------------------------------------- #
# Attribution and provenance survive the database
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_source_and_its_citation_link_are_read_back(
    migrated: AsyncEngine,
) -> None:
    """An item that cannot be cited cannot be displayed."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))

    await PollNewsFeeds([feed], factory).execute(_poll_command())
    found = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))

    stored = found[0].revision.item.source
    assert stored.key == GDELT_SOURCE_KEY
    assert stored.homepage_url == GDELT_ATTRIBUTION_URL
    assert PUBLISHER in stored.display_name


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_publishers_own_url_survives_the_round_trip(
    migrated: AsyncEngine,
) -> None:
    """DHRUVA stores the link so a reader can go and read the article."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))

    await PollNewsFeeds([feed], factory).execute(_poll_command())
    found = await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))

    assert found[0].revision.item.identity.url == f"https://{PUBLISHER}/hal-order"


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_stored_item_renders_with_everything_needed_to_cite_it(
    migrated: AsyncEngine,
) -> None:
    """The presentation contract, exercised over rows that came from PostgreSQL."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))

    await PollNewsFeeds([feed], factory).execute(_poll_command())
    rendered = render_items(await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED)))

    assert GDELT_ATTRIBUTION_URL in rendered
    assert f"https://{PUBLISHER}/hal-order" in rendered
    assert "HAL" in rendered


# --------------------------------------------------------------------------- #
# Failure, and what it must not do
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_rate_limit_keeps_what_arrived_and_asks_for_no_more(
    migrated: AsyncEngine,
) -> None:
    """Being throttled must not become data loss, through a real transaction."""
    factory = _factory(migrated)
    first = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))
    throttled = _Feed(_unhealthy(SourceHealth.RATE_LIMITED))
    never = _Feed(_healthy(_item(f"https://{PUBLISHER}/sbi-rates", BANK_HEADLINE)))

    polled = await PollNewsFeeds([first, throttled, never], factory).execute(_poll_command())

    assert never.polls == 0
    assert polled.revisions_added == 1
    assert await _count(migrated, NewsItemRevisionModel) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_pass_that_returns_nothing_writes_nothing(migrated: AsyncEngine) -> None:
    """An empty successful poll opens no transaction and stores no row."""
    factory = _factory(migrated)

    polled = await PollNewsFeeds([_Feed(_unhealthy(SourceHealth.EMPTY_RESULT))], factory).execute(
        _poll_command()
    )

    assert polled.items_offered == 0
    assert polled.unhealthy == ()
    assert await _count(migrated, NewsItemRevisionModel) == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_read_writes_nothing(migrated: AsyncEngine) -> None:
    """A query is a query. It must not commit, and must not append."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))
    await PollNewsFeeds([feed], factory).execute(_poll_command())
    before = await _count(migrated, NewsItemRevisionModel)

    for _ in range(3):
        await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))

    assert await _count(migrated, NewsItemRevisionModel) == before
    assert await _count(migrated, NewsAnalysisModel) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_failed_ingestion_leaves_the_archive_untouched(
    migrated: AsyncEngine,
) -> None:
    """The Unit of Work owns the boundary; a refused command commits nothing.

    The command is refused because one item claims to have been observed after
    the analysis that reads it, which is exactly the ordering the archive must
    never store.
    """
    factory = _factory(migrated)
    impossible = _item(
        f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE, first_seen_at=CORRECTED_SEEN
    )

    with pytest.raises(ValidationError):
        await PollNewsFeeds(
            [_Feed(_healthy(impossible, observed_at=CORRECTED_SEEN))], factory
        ).execute(_poll_command(ANALYSED))

    assert await _count(migrated, NewsItemRevisionModel) == 0


# --------------------------------------------------------------------------- #
# Bounds on the read
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_result_limit_bounds_what_comes_back(migrated: AsyncEngine) -> None:
    """An operator tool prints a page, not an export."""
    factory = _factory(migrated)
    feed = _Feed(
        _healthy(
            _item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE),
            _item(f"https://{PUBLISHER}/sbi-rates", BANK_HEADLINE),
        )
    )
    await PollNewsFeeds([feed], factory).execute(_poll_command())

    assert len(await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED, limit=1))) == 1
    assert len(await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED))) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_impossible_limit_is_refused_before_the_database_is_touched(
    migrated: AsyncEngine,
) -> None:
    """Zero results requested is a mistake, not a request."""
    factory = _factory(migrated)

    with pytest.raises(ValidationError, match="positive count"):
        await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED, limit=0))


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_backwards_window_is_refused(migrated: AsyncEngine) -> None:
    """A window that ends before it starts cannot have an answer."""
    factory = _factory(migrated)

    with pytest.raises(ValidationError, match="window"):
        await GetArchivedNews(factory).execute(
            GetArchivedNewsQuery(
                account_id=ACCOUNT,
                known_at=CORRECTED_ANALYSED,
                published_from=PUBLISHED,
                published_to=PUBLISHED - timedelta(days=1),
            )
        )


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_unknown_symbol_returns_nothing_rather_than_everything(
    migrated: AsyncEngine,
) -> None:
    """A filter that silently stopped filtering would be the dangerous failure."""
    factory = _factory(migrated)
    feed = _Feed(_healthy(_item(f"https://{PUBLISHER}/hal-order", ORDER_HEADLINE)))
    await PollNewsFeeds([feed], factory).execute(_poll_command())

    assert (
        await GetArchivedNews(factory).execute(_read(CORRECTED_ANALYSED, symbol="NOTLISTED")) == ()
    )
