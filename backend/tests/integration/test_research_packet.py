"""The research packet, built from rows that came back out of PostgreSQL.

``test_research_packet.py`` (unit) proves the serialisation. This proves the
claims that only a real database can settle: that repeating a packet export
over unchanged data produces an identical body, and that neither a news
revision nor a market bar recorded after the cutoff -- nor a correction
retrieved after it -- can reach a packet built at that cutoff. The property
this file exists to prove is exactly ``test_research_snapshot.py``'s own,
carried through the new top-N attention read path
(:func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.
rank_watchlist` / :func:`~dhruva.contexts.intelligence.domain.attention.
top_attention`) rather than the full-digest one.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.intelligence.application.news_ingestion import (
    IngestNewsItems,
    IngestNewsItemsCommand,
)
from dhruva.contexts.intelligence.application.watchlist_digest import (
    BuildWatchlistDigest,
    BuildWatchlistDigestQuery,
)
from dhruva.contexts.intelligence.domain.attention import top_attention
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
from dhruva.contexts.intelligence.domain.news import (
    NewsItem,
    NewsItemIdentity,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_SOURCE_KEY,
    gdelt_source,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    NewsItemRevisionModel,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)
from dhruva.contexts.intelligence.interfaces.attention_presentation import rank_watchlist
from dhruva.contexts.intelligence.interfaces.digest_export import serialise_snapshot
from dhruva.contexts.intelligence.interfaces.research_packet import (
    PACKET_SCHEMA_VERSION,
    build_packet,
)
from dhruva.contexts.marketdata.api import (
    GetMarketContext,
    GetMarketContextQuery,
    contexts_by_instrument,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
CUTOFF_DAY = date(2026, 8, 3)
KNOWN_AT = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
EARLY_RETRIEVAL = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
LATE_RETRIEVAL = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
FIRST_SEEN = PUBLISHED + timedelta(minutes=30)
ANALYSED = FIRST_SEEN + timedelta(minutes=5)
LATE_SEEN = FIRST_SEEN + timedelta(days=1)
LATE_ANALYSED = LATE_SEEN + timedelta(minutes=5)
GENERATED = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

FRAUD = "State Bank of India faces a forensic audit over accounting irregularities"
CORRECTED = "State Bank of India completes a forensic audit with no findings"

SBIN = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-sbin"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
HAL = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "nse-equity-hal"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
)
UNIVERSE = (SBIN, HAL)
PUBLISHER = "economictimes.indiatimes.test"


def _market_factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyMarketDataUnitOfWork(sessions, account_id=account_id)


def _news_factory(
    engine: AsyncEngine,
) -> Callable[[AccountId], SqlAlchemyIntelligenceUnitOfWork]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return lambda account_id: SqlAlchemyIntelligenceUnitOfWork(sessions, account_id=account_id)


def _bar(
    instrument: LinkableInstrument,
    trading_date: date,
    close: str,
    *,
    retrieved_at: datetime = EARLY_RETRIEVAL,
) -> DailyBarRevision:
    price = Decimal(close)
    return DailyBarRevision(
        instrument_id=instrument.instrument_id,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="kite",
        source_instrument_id=1,
        candle=DailyCandle(
            trading_date=trading_date,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=1_000,
            open_interest=None,
        ),
        retrieved_at=retrieved_at,
        adjustment_status=AdjustmentStatus.RAW,
        completeness=BarCompleteness.COMPLETE,
        source_revision=hashlib.sha256(
            f"{instrument.canonical_symbol}{trading_date}{close}{retrieved_at}".encode()
        ).hexdigest(),
        batch_sha256="b" * 64,
        quality_revision="daily-bar-quality-v1",
    )


async def _store_bars(engine: AsyncEngine, *bars: DailyBarRevision) -> None:
    async with _market_factory(engine)(ACCOUNT) as unit_of_work:
        await unit_of_work.daily_bars.add_series(DailyBarSeries(bars=tuple(bars)))
        await unit_of_work.commit()


async def _ingest(
    engine: AsyncEngine,
    path: str,
    title: str,
    *,
    first_seen_at: datetime = FIRST_SEEN,
    analysed_at: datetime = ANALYSED,
) -> None:
    link = canonical_url(f"https://{PUBLISHER}/{path}")
    item = NewsItem(
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
    await IngestNewsItems(_news_factory(engine)).execute(
        IngestNewsItemsCommand(
            account_id=ACCOUNT, items=(item,), universe=UNIVERSE, analysed_at=analysed_at
        )
    )


async def _packet(
    engine: AsyncEngine,
    *,
    known_at: datetime = KNOWN_AT,
    with_market: bool = True,
    generated_at: datetime = GENERATED,
    top: int = 5,
) -> dict[str, Any]:
    digest = await BuildWatchlistDigest(_news_factory(engine)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=known_at,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=known_at,
        )
    )
    contexts = None
    if with_market:
        contexts = contexts_by_instrument(
            await GetMarketContext(_market_factory(engine)).execute(
                GetMarketContextQuery(
                    account_id=ACCOUNT,
                    instrument_ids=tuple(entry.instrument_id for entry in UNIVERSE),
                    known_at=known_at,
                )
            )
        )
    ranked = rank_watchlist(digest, contexts)
    selected = top_attention(ranked, limit=top)
    return build_packet(
        selected,
        ranked=ranked,
        digest=digest,
        contexts=contexts,
        account_id=ACCOUNT,
        generated_at=generated_at,
        requested_top=top,
    )


def _entry(packet: dict[str, Any], symbol: str) -> dict[str, Any]:
    body: Any = packet["body"]
    return next(item for item in body["attention"] if item["canonical_symbol"] == symbol)


async def _counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as connection:
        news = await connection.execute(select(func.count()).select_from(NewsItemRevisionModel))
        bars = await connection.execute(
            select(func.count()).select_from(DailyMarketBarRevisionModel)
        )
        return int(news.scalar_one()), int(bars.scalar_one())


# --------------------------------------------------------------------------- #
# Determinism over real data
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_repeating_a_packet_export_produces_an_identical_body(
    migrated: AsyncEngine,
) -> None:
    """The contract, over rows the database ordered rather than a dict did."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )

    first = await _packet(migrated)
    second = await _packet(migrated, generated_at=GENERATED + timedelta(hours=3))

    assert serialise_snapshot({"b": first["body"]}) == serialise_snapshot({"b": second["body"]})
    assert first["envelope"]["body_sha256"] == second["envelope"]["body_sha256"]
    assert first["envelope"]["generated_at"] != second["envelope"]["generated_at"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_historical_cutoff_produces_a_valid_packet(migrated: AsyncEngine) -> None:
    """An explicit past cutoff is answered exactly as a present one is."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )

    packet = await _packet(migrated, known_at=KNOWN_AT)

    assert packet["body"]["as_of"] == KNOWN_AT.isoformat()
    assert packet["body"]["schema_version"] == PACKET_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# The cutoff is absolute, in both archives
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_post_cutoff_market_data_does_not_leak(migrated: AsyncEngine) -> None:
    """Trading date earlier, retrieval later: still invisible at the cutoff."""
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "500", retrieved_at=LATE_RETRIEVAL))
    await _ingest(migrated, "sbi-audit", FRAUD)

    at_cutoff = await _packet(migrated)
    later = await _packet(migrated, known_at=LATER)

    at_cutoff_entry = _entry(at_cutoff, "SBIN")
    later_entry = _entry(later, "SBIN")
    assert at_cutoff_entry["market"]["latest_close"] != "500"
    assert later_entry["market"]["latest_close"] == "500"


@pytest.mark.usefixtures("truncated_after_test")
async def test_post_cutoff_news_does_not_leak(migrated: AsyncEngine) -> None:
    """A packet claiming knowledge DHRUVA did not have would be worthless."""
    await _ingest(migrated, "sbi-audit", FRAUD, first_seen_at=LATE_SEEN, analysed_at=LATE_ANALYSED)

    at_cutoff = await _packet(migrated)
    later = await _packet(migrated, known_at=LATER)

    assert at_cutoff["body"]["watchlist_summary"]["with_archived_news"] == 0
    assert later["body"]["watchlist_summary"]["with_archived_news"] == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_post_cutoff_correction_does_not_rewrite_the_earlier_packet(
    migrated: AsyncEngine,
) -> None:
    """Both revisions are stored; the packet exports the knowable one, never the later."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _ingest(
        migrated, "sbi-audit", CORRECTED, first_seen_at=LATE_SEEN, analysed_at=LATE_ANALYSED
    )

    at_cutoff = _entry(await _packet(migrated), "SBIN")
    later = _entry(await _packet(migrated, known_at=LATER), "SBIN")

    assert at_cutoff["news"][0]["title"] == FRAUD
    assert later["news"][0]["title"] == CORRECTED
    assert at_cutoff["news"][0]["content_revision"] != later["news"][0]["content_revision"]


# --------------------------------------------------------------------------- #
# Ranking, top-N and score-zero policy over real data
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_ranking_order_matches_rank_watchlist_over_real_data(
    migrated: AsyncEngine,
) -> None:
    """The packet's order is exactly rank_watchlist's, not a second opinion."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )
    await _store_bars(
        migrated,
        _bar(HAL, CUTOFF_DAY - timedelta(days=1), "50"),
        _bar(HAL, CUTOFF_DAY, "50"),
    )

    packet = await _packet(migrated, top=5)
    symbols = [entry["canonical_symbol"] for entry in packet["body"]["attention"]]

    assert symbols == ["SBIN"]  # HAL is flat and quiet: score 0, excluded


@pytest.mark.usefixtures("truncated_after_test")
async def test_top_n_bounds_the_attention_array_but_not_the_summary(
    migrated: AsyncEngine,
) -> None:
    """top_attention's own truncation, proven against real rows."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _ingest(migrated, "hal-order", "Hindustan Aeronautics bags a large defence order")

    packet = await _packet(migrated, top=1)

    assert len(packet["body"]["attention"]) == 1
    assert packet["body"]["watchlist_summary"]["instruments"] == len(UNIVERSE)
    assert packet["body"]["top_n"] == 1


# --------------------------------------------------------------------------- #
# Attribution, absence, and read-only behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_attribution_survives_the_round_trip(migrated: AsyncEngine) -> None:
    """An item that cannot be cited cannot be exported."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    entry = _entry(await _packet(migrated), "SBIN")["news"][0]

    assert entry["source"]["key"] == GDELT_SOURCE_KEY
    assert entry["source"]["attribution_url"] == "https://gdeltproject.org"
    assert entry["url"] == f"https://{PUBLISHER}/sbi-audit"


@pytest.mark.usefixtures("truncated_after_test")
async def test_market_context_omitted_is_distinguishable_from_absent_data(
    migrated: AsyncEngine,
) -> None:
    """A news-only packet must not read as evidence that no prices existed."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    packet = await _packet(migrated, with_market=False)

    assert packet["body"]["market_context_requested"] is False
    assert _entry(packet, "SBIN")["market"] == {"requested": False, "availability": None}
    assert _entry(packet, "SBIN")["market_context_available"] is False


@pytest.mark.usefixtures("truncated_after_test")
async def test_exporting_a_packet_writes_nothing_to_the_database(migrated: AsyncEngine) -> None:
    """It is a query. Repeating it must not change the archive."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "110"))
    before = await _counts(migrated)

    for _ in range(3):
        await _packet(migrated)

    assert await _counts(migrated) == before


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_packet_carries_no_credentials_or_local_paths(migrated: AsyncEngine) -> None:
    """A packet is a file somebody may email; it must be safe to send."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "110"))

    text = serialise_snapshot(await _packet(migrated)).lower()

    for forbidden in (
        "password",
        "secret",
        "api_key",
        "access_token",
        "master_key",
        "c:\\",
        "/home/",
    ):
        assert forbidden not in text
