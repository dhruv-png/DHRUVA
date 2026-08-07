"""The digest, composed from rows that came back out of PostgreSQL.

The unit suite proves the grouping rules against in-memory items. This proves
the rules still hold over values the database round-tripped, and — the part
that cannot be faked — that the point-in-time cutoff survives into the digest
rather than being applied only at the read.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.events import EventCategory
from dhruva.contexts.intelligence.domain.news import (
    NewsItem,
    NewsItemIdentity,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_ATTRIBUTION_URL,
    GDELT_SOURCE_KEY,
    gdelt_source,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.digest_presentation import render_digest
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
FIRST_SEEN = PUBLISHED + timedelta(minutes=30)
ANALYSED = FIRST_SEEN + timedelta(minutes=5)
LATER_SEEN = FIRST_SEEN + timedelta(hours=6)
LATER_ANALYSED = LATER_SEEN + timedelta(minutes=5)

ORDER = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"
FRAUD = "State Bank of India faces a forensic audit over accounting irregularities"

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
PNB = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-pnb"),
    canonical_symbol="PNB",
    company_name="Punjab National Bank",
)
UNIVERSE = (HAL, SBIN, PNB)

PUBLISHER = "economictimes.indiatimes.test"


def _factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyIntelligenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


def _item(path: str, title: str, *, first_seen_at: datetime = FIRST_SEEN) -> NewsItem:
    link = canonical_url(f"https://{PUBLISHER}/{path}")
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT_SOURCE_KEY,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=gdelt_source(PUBLISHER),
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=first_seen_at,
    )


async def _ingest(engine: AsyncEngine, *items: NewsItem, analysed_at: datetime = ANALYSED) -> None:
    await IngestNewsItems(_factory(engine)).execute(
        IngestNewsItemsCommand(
            account_id=ACCOUNT, items=items, universe=UNIVERSE, analysed_at=analysed_at
        )
    )


async def _digest(engine: AsyncEngine, *, known_at: datetime = LATER_ANALYSED, **kwargs: object):  # type: ignore[no-untyped-def]
    return await BuildWatchlistDigest(_factory(engine)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=known_at,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=PUBLISHED + timedelta(days=1),
            **kwargs,  # type: ignore[arg-type]
        )
    )


def _section(digest, symbol: str):  # type: ignore[no-untyped-def]
    return next(s for s in digest.sections if s.canonical_symbol == symbol)


@pytest.mark.usefixtures("truncated_after_test")
async def test_ingested_news_appears_under_the_instrument_it_linked_to(
    migrated: AsyncEngine,
) -> None:
    """The whole read path, over rows PostgreSQL actually stored and returned."""
    await _ingest(migrated, _item("hal-order", ORDER))

    digest = await _digest(migrated)

    assert len(_section(digest, "HAL").entries) == 1
    assert _section(digest, "SBIN").is_quiet


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_stored_category_and_sentiment_survive_the_round_trip(
    migrated: AsyncEngine,
) -> None:
    """A digest that recomputed could disagree with what the archive holds."""
    await _ingest(migrated, _item("sbi-audit", FRAUD))

    entry = _section(await _digest(migrated), "SBIN").entries[0]

    assert entry.category is EventCategory.FRAUD_GOVERNANCE
    assert entry.item.revision.item.source.homepage_url == GDELT_ATTRIBUTION_URL


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_item_first_seen_after_the_cutoff_is_absent_from_the_digest(
    migrated: AsyncEngine,
) -> None:
    """The point-in-time rule has to survive into the summary, not stop at the read.

    A digest that leaked a later observation would be the exact failure the
    bitemporal schema exists to prevent, dressed up as a summary.
    """
    await _ingest(
        migrated,
        _item("sbi-audit", FRAUD, first_seen_at=LATER_SEEN),
        analysed_at=LATER_ANALYSED,
    )

    early = await _digest(migrated, known_at=ANALYSED)
    late = await _digest(migrated, known_at=LATER_ANALYSED + timedelta(minutes=1))

    assert _section(early, "SBIN").is_quiet
    assert len(_section(late, "SBIN").entries) == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_sections_are_ordered_by_significance_over_real_rows(
    migrated: AsyncEngine,
) -> None:
    """A governance finding above an order win, and both above silence."""
    await _ingest(migrated, _item("hal-order", ORDER), _item("sbi-audit", FRAUD))

    digest = await _digest(migrated)

    assert [s.canonical_symbol for s in digest.sections] == ["SBIN", "HAL", "PNB"]
    assert digest.sections[-1].is_quiet


@pytest.mark.usefixtures("truncated_after_test")
async def test_every_watchlist_instrument_is_present_even_with_an_empty_archive(
    migrated: AsyncEngine,
) -> None:
    """A quiet day is an answer, and every instrument still gets its section."""
    digest = await _digest(migrated)

    assert len(digest.sections) == len(UNIVERSE)
    assert len(digest.quiet) == len(UNIVERSE)
    assert digest.items_reported == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_rendered_digest_carries_attribution_for_stored_rows(
    migrated: AsyncEngine,
) -> None:
    """GDELT's terms attach a citation to using the data at all."""
    await _ingest(migrated, _item("hal-order", ORDER))

    rendered = render_digest(await _digest(migrated))

    assert GDELT_ATTRIBUTION_URL in rendered
    assert f"https://{PUBLISHER}/hal-order" in rendered
    assert "NSE" in rendered


@pytest.mark.usefixtures("truncated_after_test")
async def test_building_a_digest_writes_nothing(migrated: AsyncEngine) -> None:
    """It is a query. Repeating it must not change the archive or the answer."""
    await _ingest(migrated, _item("hal-order", ORDER))

    first = await _digest(migrated)
    second = await _digest(migrated)

    assert first == second
    assert first.items_reported == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_section_bound_applies_over_stored_rows(migrated: AsyncEngine) -> None:
    """Truncation is reported rather than hidden, whatever the source of the rows."""
    await _ingest(
        migrated,
        *(_item(f"hal-{n}", f"{ORDER} number {n}") for n in range(4)),
    )

    section = _section(await _digest(migrated, max_items=2), "HAL")

    assert len(section.entries) == 2
    assert section.withheld == 2
