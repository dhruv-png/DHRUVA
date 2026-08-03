"""Point-in-time news persistence against real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    GetArchivedNews,
    GetArchivedNewsQuery,
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.domain.archive import NewsRevision, content_revision
from dhruva.contexts.intelligence.domain.entity_linking import (
    LinkableInstrument,
    MatchKind,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import EventCategory
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    DeduplicationRule,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sentiment import SentimentLabel
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    NewsAnalysisModel,
    NewsEntityLinkModel,
    NewsItemRevisionModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
FIRST_SEEN = PUBLISHED + timedelta(minutes=30)
ANALYSED = PUBLISHED + timedelta(hours=1)
CORRECTED_AT = ANALYSED + timedelta(hours=2)
ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"
FILING_HEADLINE = "Board Meeting Intimation for Financial Results"

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
    aliases=("SBI",),
)
UNIVERSE = (HAL, SBIN)

FILINGS = NewsSource(
    key="nse-announcements",
    display_name="NSE Corporate Announcements",
    tier=NewsSourceTier.OFFICIAL_FILING,
    homepage_url="https://nseindia.com",
)
WIRE = NewsSource(
    key="wire-a",
    display_name="Wire A",
    tier=NewsSourceTier.ESTABLISHED_PUBLISHER,
    homepage_url="https://wire-a.example",
)
OTHER_WIRE = NewsSource(
    key="wire-b",
    display_name="Wire B",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://wire-b.example",
)


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyIntelligenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


def _item(  # noqa: PLR0913 - each argument varies one axis these tests exercise
    source: NewsSource,
    provider_item_id: str,
    url: str,
    title: str,
    *,
    published_at: datetime = PUBLISHED,
    first_seen_at: datetime = FIRST_SEEN,
    snippet: str | None = None,
) -> NewsItem:
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=source.key,
            provider_item_id=provider_item_id,
            url=canonical_url(url),
        ),
        source=source,
        text=PermittedText(title=title, snippet=snippet),
        published_at=published_at,
        first_seen_at=first_seen_at,
    )


def _command(*items: NewsItem, analysed_at: datetime = ANALYSED) -> IngestNewsItemsCommand:
    return IngestNewsItemsCommand(
        account_id=ACCOUNT,
        items=items,
        universe=UNIVERSE,
        analysed_at=analysed_at,
    )


def _query(known_at: datetime, *, canonical_symbol: str | None = None) -> GetArchivedNewsQuery:
    return GetArchivedNewsQuery(
        account_id=ACCOUNT,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=2),
        published_to=PUBLISHED + timedelta(days=2),
        canonical_symbol=canonical_symbol,
    )


async def _count(engine: AsyncEngine, model: type) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_poll_persists_the_revision_its_analysis_and_its_links(
    migrated: AsyncEngine,
) -> None:
    """One observation becomes one row in each of the three tables."""
    factory = _factory(migrated)

    result = await IngestNewsItems(factory).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))
    )

    assert result.revisions_added == 1
    assert result.analyses_added == 1
    assert result.links_added == 1
    assert await _count(migrated, NewsItemRevisionModel) == 1
    assert await _count(migrated, NewsAnalysisModel) == 1
    assert await _count(migrated, NewsEntityLinkModel) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_repolling_is_idempotent_and_preserves_the_first_observation(
    migrated: AsyncEngine,
) -> None:
    """The second poll must not claim DHRUVA saw the item for the first time today."""
    factory = _factory(migrated)
    first = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    later = _item(
        WIRE,
        "1",
        "https://wire-a.example/1",
        ORDER_HEADLINE,
        first_seen_at=FIRST_SEEN + timedelta(days=1),
    )

    await IngestNewsItems(factory).execute(_command(first))
    repeat = await IngestNewsItems(factory).execute(
        _command(later, analysed_at=ANALYSED + timedelta(days=1))
    )

    assert repeat.revisions_added == 0
    assert repeat.revisions_unchanged == 1
    assert repeat.analyses_added == 0
    assert await _count(migrated, NewsItemRevisionModel) == 1

    async with migrated.connect() as connection:
        stored = await connection.execute(select(NewsItemRevisionModel.first_seen_at))
        assert stored.scalar_one() == FIRST_SEEN


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_correction_appends_and_cannot_overwrite_earlier_history(
    migrated: AsyncEngine,
) -> None:
    """A read at the earlier cutoff still returns the wording that was published then."""
    factory = _factory(migrated)
    original = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    corrected = _item(
        WIRE,
        "1",
        "https://wire-a.example/1",
        "Hindustan Aeronautics bags order worth Rs 6,000 crore from the ministry",
        first_seen_at=CORRECTED_AT,
    )

    await IngestNewsItems(factory).execute(_command(original))
    await IngestNewsItems(factory).execute(_command(corrected, analysed_at=CORRECTED_AT))

    assert await _count(migrated, NewsItemRevisionModel) == 2
    before = await GetArchivedNews(factory).execute(_query(ANALYSED))
    after = await GetArchivedNews(factory).execute(_query(CORRECTED_AT))

    assert len(before) == 1
    assert before[0].revision.item.text.title == ORDER_HEADLINE
    assert before[0].revision.revision == content_revision(original)
    assert len(after) == 1
    assert after[0].revision.item.text.title == corrected.text.title


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_item_observed_after_the_cutoff_is_invisible(migrated: AsyncEngine) -> None:
    """The knowledge-time read is what stops a backtest reading tomorrow's news."""
    factory = _factory(migrated)
    await IngestNewsItems(factory).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))
    )

    assert await GetArchivedNews(factory).execute(_query(FIRST_SEEN - timedelta(minutes=1))) == ()
    assert len(await GetArchivedNews(factory).execute(_query(FIRST_SEEN))) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_stored_analysis_round_trips_with_its_evidence(
    migrated: AsyncEngine,
) -> None:
    """What comes back out is the same verdict, terms and provenance that went in."""
    factory = _factory(migrated)
    await IngestNewsItems(factory).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))
    )

    archived = await GetArchivedNews(factory).execute(_query(ANALYSED))

    assert len(archived) == 1
    analysis = archived[0].analysis
    assert analysis is not None
    assert analysis.event.category is EventCategory.ORDER_WIN
    assert analysis.sentiment.label is SentimentLabel.POSITIVE
    assert "bags" in analysis.sentiment.positive_terms
    assert analysis.analysed_at == ANALYSED

    # Compare against the linker itself rather than a hand-written expectation.
    # "Round-trips exactly" is a statement about persistence, and the only way to
    # assert it without also asserting a second, hand-copied opinion about what
    # the linker should have said is to ask the linker.
    expected = link_entities(
        ORDER_HEADLINE,
        universe=UNIVERSE,
        published_on=PUBLISHED.date(),
    )
    assert analysis.mapping.matches == expected.matches
    assert analysis.mapping.ambiguous == expected.ambiguous
    assert analysis.mapping.state is expected.state
    assert analysis.mapping.revision == expected.revision

    match = analysis.mapping.matches[0]
    assert match.instrument_id == HAL.instrument_id
    assert match.canonical_symbol == "HAL"
    # COMPANY_NAME, not ALIAS. "Hindustan Aeronautics" is reachable both ways
    # here -- it is the registered name minus its corporate suffix *and* a listed
    # alias -- and the registered name is the stronger evidence, so precedence
    # gives it 0.90 rather than an alias's 0.80. `test_entity_linking.py` pins
    # the alias path separately on SBIN, whose company name carries no suffix to
    # strip and so can only be reached through "SBI".
    assert match.kind is MatchKind.COMPANY_NAME
    assert match.relevance == Decimal("0.90")


@pytest.mark.usefixtures("truncated_after_test")
async def test_deduplication_survives_a_restart(migrated: AsyncEngine) -> None:
    """Stored fingerprints are what stop a fresh process re-admitting syndication."""
    factory = _factory(migrated)
    await IngestNewsItems(factory).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))
    )

    second = await IngestNewsItems(factory).execute(
        _command(
            _item(OTHER_WIRE, "9", "https://wire-b.example/9", ORDER_HEADLINE.upper()),
            analysed_at=ANALYSED + timedelta(minutes=5),
        )
    )

    assert second.duplicates == 1
    assert second.revisions_added == 1
    assert second.analyses_added == 0
    async with migrated.connect() as connection:
        rows = await connection.execute(
            select(
                NewsItemRevisionModel.duplicate_rule,
                NewsItemRevisionModel.duplicate_of_source_key,
                NewsItemRevisionModel.duplicate_of_provider_item_id,
            ).where(NewsItemRevisionModel.provider_item_id == "9")
        )
        rule, source_key, provider_item_id = rows.one()
    assert rule == DeduplicationRule.SYNDICATED_HEADLINE.value
    assert source_key == WIRE.key
    assert provider_item_id == "1"


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_repeated_official_filing_is_stored_with_its_attribution(
    migrated: AsyncEngine,
) -> None:
    """The exchange republishes a filing; the archive says which one it repeats."""
    factory = _factory(migrated)

    result = await IngestNewsItems(factory).execute(
        _command(
            _item(
                FILINGS, "F1", "https://nseindia.com/f/1", "Board Meeting Intimation for Results"
            ),
            _item(
                FILINGS, "F2", "https://nseindia.com/f/2", "Board Meeting Intimation for Results"
            ),
        )
    )

    assert result.duplicates == 1
    assert await _count(migrated, NewsItemRevisionModel) == 2
    assert await _count(migrated, NewsAnalysisModel) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_read_can_be_narrowed_to_one_canonical_symbol(migrated: AsyncEngine) -> None:
    """An instrument page asks for its own news, not for the whole day."""
    factory = _factory(migrated)
    await IngestNewsItems(factory).execute(
        _command(
            _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE),
            _item(WIRE, "2", "https://wire-a.example/2", "State Bank of India cuts lending rates"),
        )
    )

    hal = await GetArchivedNews(factory).execute(_query(ANALYSED, canonical_symbol="HAL"))
    sbin = await GetArchivedNews(factory).execute(_query(ANALYSED, canonical_symbol="SBIN"))

    assert len(hal) == 1
    assert len(sbin) == 1
    assert hal[0].revision.item.text.title == ORDER_HEADLINE


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_transaction_rolls_back_by_default(migrated: AsyncEngine) -> None:
    """A unit of work that stages real rows and never commits leaves nothing behind.

    Staging an actual revision matters: appending nothing would pass even if
    rollback had stopped working, which would make this test agree with a
    defect rather than catch it.
    """
    sessions = async_sessionmaker(migrated, expire_on_commit=False)
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    revision = NewsRevision(
        item=item,
        revision=content_revision(item),
        deduplication=DeduplicationDecision(
            is_duplicate=False,
            rule=None,
            original=None,
            reason="no bounded rule matched an item already observed",
        ),
    )

    async with SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=ACCOUNT) as unit_of_work:
        write = await unit_of_work.news.append(((revision, None),))
        assert write.revisions_added == 1

    assert await _count(migrated, NewsItemRevisionModel) == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_database_refuses_an_article_body(migrated: AsyncEngine) -> None:
    """The licence bound is enforced by the schema, not only by the domain type."""
    sessions = async_sessionmaker(migrated, expire_on_commit=False)
    body = "x" * 4000

    async with sessions() as session:
        with pytest.raises(Exception, match="ck_news_item_snippet_bounds"):
            await session.execute(
                insert(NewsItemRevisionModel).values(
                    id=InstrumentId.deterministic("test", "body").value,
                    source_key="wire-a",
                    source_display_name="Wire A",
                    source_tier="ESTABLISHED_PUBLISHER",
                    source_homepage_url="https://wire-a.example",
                    provider_item_id="1",
                    canonical_url="https://wire-a.example/1",
                    title=ORDER_HEADLINE,
                    snippet=body,
                    published_at=PUBLISHED,
                    first_seen_at=FIRST_SEEN,
                    content_revision="0" * 64,
                    fingerprint_identity="0" * 64,
                    fingerprint_url="0" * 64,
                    fingerprint_headline="0" * 64,
                    fingerprint_rewrite=None,
                    duplicate_rule=None,
                    duplicate_of_source_key=None,
                    duplicate_of_provider_item_id=None,
                    duplicate_of_url=None,
                    duplicate_reason="none",
                    identity_revision="news-identity-v1",
                )
            )


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_database_refuses_an_item_observed_before_publication(
    migrated: AsyncEngine,
) -> None:
    """A clock defect must fail at the schema, where no code path can talk past it."""
    sessions = async_sessionmaker(migrated, expire_on_commit=False)

    async with sessions() as session:
        with pytest.raises(Exception, match="ck_news_item_observed_after_publication"):
            await session.execute(
                insert(NewsItemRevisionModel).values(
                    id=InstrumentId.deterministic("test", "clock").value,
                    source_key="wire-a",
                    source_display_name="Wire A",
                    source_tier="ESTABLISHED_PUBLISHER",
                    source_homepage_url="https://wire-a.example",
                    provider_item_id="2",
                    canonical_url="https://wire-a.example/2",
                    title=ORDER_HEADLINE,
                    snippet=None,
                    published_at=PUBLISHED,
                    first_seen_at=PUBLISHED - timedelta(hours=1),
                    content_revision="0" * 64,
                    fingerprint_identity="0" * 64,
                    fingerprint_url="0" * 64,
                    fingerprint_headline="0" * 64,
                    fingerprint_rewrite=None,
                    duplicate_rule=None,
                    duplicate_of_source_key=None,
                    duplicate_of_provider_item_id=None,
                    duplicate_of_url=None,
                    duplicate_reason="none",
                    identity_revision="news-identity-v1",
                )
            )


@pytest.mark.usefixtures("truncated_after_test")
async def test_two_polls_on_different_days_are_separate_events(migrated: AsyncEngine) -> None:
    """The same sentence in two quarters is not one announcement."""
    factory = _factory(migrated)
    today = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    tomorrow = _item(
        WIRE,
        "2",
        "https://wire-a.example/2",
        ORDER_HEADLINE,
        published_at=PUBLISHED + timedelta(days=1),
        first_seen_at=FIRST_SEEN + timedelta(days=1),
    )

    await IngestNewsItems(factory).execute(_command(today))
    second = await IngestNewsItems(factory).execute(
        _command(tomorrow, analysed_at=ANALYSED + timedelta(days=1))
    )

    assert second.duplicates == 0
    assert second.analyses_added == 1
    assert await _count(migrated, NewsItemRevisionModel) == 2


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_published_date_is_what_the_linker_reads(migrated: AsyncEngine) -> None:
    """Mapping happens as of publication, not as of whenever the poll ran."""
    factory = _factory(migrated)
    await IngestNewsItems(factory).execute(
        _command(
            _item(
                WIRE,
                "1",
                "https://wire-a.example/1",
                ORDER_HEADLINE,
                published_at=datetime.combine(date(2026, 8, 3), PUBLISHED.timetz()),
            )
        )
    )

    archived = await GetArchivedNews(factory).execute(_query(ANALYSED))

    assert archived[0].analysis is not None
    assert archived[0].analysis.mapping.matches[0].canonical_symbol == "HAL"
