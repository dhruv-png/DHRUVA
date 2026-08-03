"""Idempotent, point-in-time news ingestion over a fake archive."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from dhruva.contexts.intelligence.application.news_ingestion import (
    GetArchivedNews,
    GetArchivedNewsQuery,
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsArchiveWrite,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.entity_linking import HistoricalName, LinkableInstrument
from dhruva.contexts.intelligence.domain.events import EventCategory
from dhruva.contexts.intelligence.domain.news import (
    NEWS_IDENTITY_REVISION,
    DeduplicationRule,
    NewsFingerprints,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sentiment import SentimentLabel
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ACCOUNT = AccountId.deterministic("owner-family")
PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
ANALYSED = datetime(2026, 8, 3, 6, 30, tzinfo=UTC)
ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"

HAL = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
    aliases=("Hindustan Aeronautics",),
    former_names=(HistoricalName("Old Aero Works", date(2026, 6, 30)),),
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


def _item(  # noqa: PLR0913 - each argument varies one axis these tests exercise
    source: NewsSource,
    provider_item_id: str,
    url: str,
    title: str,
    *,
    published_at: datetime = PUBLISHED,
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
        first_seen_at=published_at + timedelta(minutes=30),
    )


class FakeNewsStore:
    """An in-memory archive that reproduces the real append semantics."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[NewsRevision, NewsAnalysis | None]] = {}
        self.fingerprint_calls: list[tuple[datetime, datetime]] = []

    @staticmethod
    def _key(revision: NewsRevision) -> tuple[str, str, str]:
        return (
            revision.item.identity.source_key,
            revision.item.identity.provider_item_id,
            revision.revision,
        )

    async def append(
        self,
        revisions: tuple[tuple[NewsRevision, NewsAnalysis | None], ...],
    ) -> NewsArchiveWrite:
        """Insert unseen rows and leave an earlier observation exactly as it was."""
        added = 0
        analyses = 0
        links = 0
        for revision, analysis in revisions:
            key = self._key(revision)
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
        """Return the latest revision per item observable at the cutoff."""
        latest: dict[tuple[str, str], tuple[NewsRevision, NewsAnalysis | None]] = {}
        for revision, analysis in self.rows.values():
            item = revision.item
            if item.first_seen_at > known_at:
                continue
            if not published_from <= item.published_at <= published_to:
                continue
            key = (item.identity.source_key, item.identity.provider_item_id)
            prior = latest.get(key)
            if prior is None or item.first_seen_at > prior[0].item.first_seen_at:
                latest[key] = (revision, analysis)
        selected = [
            ArchivedNewsItem(revision=revision, analysis=analysis)
            for revision, analysis in latest.values()
            if canonical_symbol is None
            or (
                analysis is not None
                and any(
                    match.canonical_symbol == canonical_symbol for match in analysis.mapping.matches
                )
            )
        ]
        return tuple(sorted(selected, key=lambda entry: entry.revision.item.published_at))

    async def fingerprints_seen_since(
        self,
        *,
        published_from: datetime,
        known_at: datetime,
    ) -> tuple[tuple[NewsItemIdentity, NewsFingerprints], ...]:
        """Return the fingerprints of non-duplicate rows inside the window."""
        self.fingerprint_calls.append((published_from, known_at))
        return tuple(
            (revision.item.identity, revision.item.fingerprints)
            for revision, _ in self.rows.values()
            if revision.item.published_at >= published_from
            and revision.item.first_seen_at <= known_at
            and not revision.deduplication.is_duplicate
        )


class FakeUnitOfWork:
    """Expose one in-memory store and record the transaction outcome."""

    def __init__(self, store: FakeNewsStore) -> None:
        self.news = store
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> Self:
        """Open the fake transaction."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Record rollback-by-default behavior."""
        if not self.commits:
            self.rollbacks += 1

    async def commit(self) -> None:
        """Record a commit."""
        self.commits += 1

    async def rollback(self) -> None:
        """Record an explicit rollback."""
        self.rollbacks += 1


class Factory:
    """Retain created transactions so write-after-validation is observable."""

    def __init__(self, store: FakeNewsStore) -> None:
        self.store = store
        self.created: list[FakeUnitOfWork] = []

    def __call__(self, account_id: AccountId) -> FakeUnitOfWork:
        """Create a transaction for the arranged account."""
        assert account_id == ACCOUNT
        unit_of_work = FakeUnitOfWork(self.store)
        self.created.append(unit_of_work)
        return unit_of_work


def _command(*items: NewsItem, analysed_at: datetime = ANALYSED) -> IngestNewsItemsCommand:
    return IngestNewsItemsCommand(
        account_id=ACCOUNT,
        items=items,
        universe=UNIVERSE,
        analysed_at=analysed_at,
    )


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


async def test_one_poll_stores_the_item_its_analysis_and_its_links() -> None:
    """The three rulesets run once and land beside the revision they describe."""
    store = FakeNewsStore()
    factory = Factory(store)
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    result = await IngestNewsItems(factory).execute(_command(item))

    assert result.items_observed == 1
    assert result.revisions_added == 1
    assert result.analyses_added == 1
    assert result.links_added == 1
    assert result.duplicates == 0
    assert factory.created[0].commits == 1

    stored, analysis = next(iter(store.rows.values()))
    assert stored.revision == content_revision(item)
    assert analysis is not None
    assert analysis.event.category is EventCategory.ORDER_WIN
    assert analysis.sentiment.label is SentimentLabel.POSITIVE
    assert tuple(match.canonical_symbol for match in analysis.mapping.matches) == ("HAL",)


async def test_repolling_the_same_items_adds_nothing() -> None:
    """A feed poll must be safe to run as often as it likes."""
    store = FakeNewsStore()
    use_case = IngestNewsItems(Factory(store))
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    first = await use_case.execute(_command(item))
    second = await use_case.execute(_command(item))

    assert first.revisions_added == 1
    assert second.revisions_added == 0
    assert second.revisions_unchanged == 1
    assert second.analyses_added == 0
    assert len(store.rows) == 1


async def test_a_repoll_does_not_move_the_first_seen_timestamp() -> None:
    """The archive must not claim it saw everything for the first time today."""
    store = FakeNewsStore()
    use_case = IngestNewsItems(Factory(store))
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    later = _item(
        WIRE,
        "1",
        "https://wire-a.example/1",
        ORDER_HEADLINE,
    )

    await use_case.execute(_command(item))
    await use_case.execute(
        _command(later, analysed_at=ANALYSED + timedelta(days=1)),
    )

    stored, _ = next(iter(store.rows.values()))
    assert stored.item.first_seen_at == item.first_seen_at


async def test_a_correction_appends_a_revision_and_leaves_the_original() -> None:
    """A later correction cannot overwrite the wording a backtest already read."""
    store = FakeNewsStore()
    use_case = IngestNewsItems(Factory(store))
    original = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    corrected = _item(
        WIRE,
        "1",
        "https://wire-a.example/1",
        "Hindustan Aeronautics bags order worth Rs 6,000 crore from the ministry",
    )

    await use_case.execute(_command(original))
    await use_case.execute(_command(corrected, analysed_at=ANALYSED + timedelta(hours=2)))

    assert len(store.rows) == 2
    revisions = {revision.revision for revision, _ in store.rows.values()}
    assert revisions == {content_revision(original), content_revision(corrected)}


async def test_duplicates_are_recorded_with_their_rule_and_not_re_analysed() -> None:
    """A repeat carries the verdict that judged it, so its absence is explainable."""
    store = FakeNewsStore()
    syndicated = _item(OTHER_WIRE, "9", "https://wire-b.example/9", ORDER_HEADLINE.upper())

    result = await IngestNewsItems(Factory(store)).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE), syndicated)
    )

    assert result.duplicates == 1
    assert result.revisions_added == 2
    assert result.analyses_added == 1
    stored = {
        revision.item.identity.provider_item_id: (revision, analysis)
        for revision, analysis in store.rows.values()
    }
    repeat, repeat_analysis = stored["9"]
    assert repeat.deduplication.rule is DeduplicationRule.SYNDICATED_HEADLINE
    assert repeat.deduplication.original is not None
    assert repeat_analysis is None


async def test_deduplication_reaches_across_polls() -> None:
    """Without the stored fingerprints, a restart would re-admit yesterday's wire."""
    store = FakeNewsStore()
    use_case = IngestNewsItems(Factory(store))

    await use_case.execute(_command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)))
    second = await use_case.execute(
        _command(_item(OTHER_WIRE, "9", "https://wire-b.example/9", ORDER_HEADLINE))
    )

    assert second.duplicates == 1
    assert store.fingerprint_calls


async def test_the_fingerprint_window_is_bounded_by_the_earliest_item() -> None:
    """Loading the whole archive to compare one day against it is work nothing uses."""
    store = FakeNewsStore()
    earlier = PUBLISHED - timedelta(days=2)

    await IngestNewsItems(Factory(store)).execute(
        _command(
            _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE, published_at=earlier),
            _item(WIRE, "2", "https://wire-a.example/2", "State Bank of India cuts rates"),
        )
    )

    assert store.fingerprint_calls == [(earlier, ANALYSED)]


async def test_an_unmapped_headline_is_stored_and_counted() -> None:
    """News about nothing DHRUVA follows is still evidence about the day."""
    store = FakeNewsStore()

    result = await IngestNewsItems(Factory(store)).execute(
        _command(
            _item(WIRE, "1", "https://wire-a.example/1", "Some unrelated firm reports results")
        )
    )

    assert result.unresolved == 1
    assert result.revisions_added == 1
    _, analysis = next(iter(store.rows.values()))
    assert analysis is not None
    assert analysis.mapping.matches == ()


async def test_the_snippet_is_read_alongside_the_headline() -> None:
    """A permitted extract is evidence too, and the rulesets are given both."""
    store = FakeNewsStore()

    await IngestNewsItems(Factory(store)).execute(
        _command(
            _item(
                WIRE,
                "1",
                "https://wire-a.example/1",
                "Defence major wins a large tender",
                snippet="Hindustan Aeronautics secured the contract, the ministry said.",
            )
        )
    )

    _, analysis = next(iter(store.rows.values()))
    assert analysis is not None
    assert tuple(match.canonical_symbol for match in analysis.mapping.matches) == ("HAL",)


async def test_an_official_filing_repeated_by_its_own_source_is_a_repeat() -> None:
    """The exchange republishes a filing; attribution stays with the first."""
    store = FakeNewsStore()

    result = await IngestNewsItems(Factory(store)).execute(
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


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


async def test_an_empty_poll_is_refused_before_any_transaction() -> None:
    """Nothing to ingest is a caller error, not an empty success."""
    factory = Factory(FakeNewsStore())

    with pytest.raises(ValidationError, match="must contain items"):
        await IngestNewsItems(factory).execute(_command())

    assert factory.created == []


async def test_an_item_observed_after_the_analysis_is_refused() -> None:
    """An analysis cannot read an observation that had not happened yet."""
    factory = Factory(FakeNewsStore())
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    with pytest.raises(ValidationError, match="cannot be observed after"):
        await IngestNewsItems(factory).execute(
            _command(item, analysed_at=item.first_seen_at - timedelta(minutes=1))
        )

    assert factory.created == []


async def test_a_naive_analysis_time_is_refused() -> None:
    """A knowledge time without a zone is not an instant (ADR-006)."""
    factory = Factory(FakeNewsStore())

    with pytest.raises(ValidationError, match="analysed_at must be timezone-aware"):
        await IngestNewsItems(factory).execute(
            _command(
                _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE),
                analysed_at=datetime(2026, 8, 3, 6, 30),  # noqa: DTZ001 - the defect under test
            )
        )

    assert factory.created == []


async def test_the_same_revision_twice_in_one_poll_is_refused() -> None:
    """One batch cannot contain the same row twice; the store would reject it."""
    factory = Factory(FakeNewsStore())
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    with pytest.raises(ValidationError, match="same item revision twice"):
        await IngestNewsItems(factory).execute(_command(item, item))

    assert factory.created == []


# --------------------------------------------------------------------------- #
# Point-in-time reads
# --------------------------------------------------------------------------- #


async def test_a_read_cannot_see_an_item_observed_after_its_cutoff() -> None:
    """Both timestamps exist precisely so this read can refuse to see too much."""
    store = FakeNewsStore()
    factory = Factory(store)
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    await IngestNewsItems(factory).execute(_command(item))

    before = await GetArchivedNews(factory).execute(
        GetArchivedNewsQuery(
            account_id=ACCOUNT,
            known_at=item.first_seen_at - timedelta(minutes=1),
            published_from=PUBLISHED - timedelta(days=1),
            published_to=PUBLISHED + timedelta(days=1),
        )
    )
    after = await GetArchivedNews(factory).execute(
        GetArchivedNewsQuery(
            account_id=ACCOUNT,
            known_at=item.first_seen_at,
            published_from=PUBLISHED - timedelta(days=1),
            published_to=PUBLISHED + timedelta(days=1),
        )
    )

    assert before == ()
    assert len(after) == 1
    assert after[0].revision.item.text.title == ORDER_HEADLINE
    assert after[0].analysis is not None


async def test_a_read_can_be_narrowed_to_one_canonical_symbol() -> None:
    """An instrument page asks for its own news, not for the whole day."""
    store = FakeNewsStore()
    factory = Factory(store)
    await IngestNewsItems(factory).execute(
        _command(
            _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE),
            _item(WIRE, "2", "https://wire-a.example/2", "State Bank of India cuts lending rates"),
        )
    )

    hal = await GetArchivedNews(factory).execute(
        GetArchivedNewsQuery(
            account_id=ACCOUNT,
            known_at=ANALYSED,
            published_from=PUBLISHED - timedelta(days=1),
            published_to=PUBLISHED + timedelta(days=1),
            canonical_symbol="HAL",
        )
    )

    assert len(hal) == 1
    assert hal[0].analysis is not None
    assert tuple(match.canonical_symbol for match in hal[0].analysis.mapping.matches) == ("HAL",)


async def test_the_stored_identity_revision_is_carried_through() -> None:
    """A fingerprint produced by older normalisation must be visibly older."""
    store = FakeNewsStore()
    await IngestNewsItems(Factory(store)).execute(
        _command(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))
    )

    revision, _ = next(iter(store.rows.values()))
    assert revision.item.fingerprints.revision == NEWS_IDENTITY_REVISION
    assert revision.deduplication.revision == NEWS_IDENTITY_REVISION
