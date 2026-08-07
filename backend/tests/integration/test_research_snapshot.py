"""The snapshot, built from rows that came back out of PostgreSQL.

The unit suite proves the serialisation. This proves the claims that only a real
database can settle: that repeating an export over unchanged data produces an
identical body, and that neither a news revision nor a market bar recorded after
the cutoff can reach a snapshot taken at it.
"""

from __future__ import annotations

import hashlib
import json
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
from dhruva.contexts.intelligence.interfaces.digest_export import (
    EXPORT_SCHEMA_VERSION,
    body_fingerprint,
    build_snapshot,
    serialise_snapshot,
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


async def _snapshot(
    engine: AsyncEngine,
    *,
    known_at: datetime = KNOWN_AT,
    with_market: bool = True,
    generated_at: datetime = GENERATED,
    max_items: int = 5,
) -> dict[str, Any]:
    digest = await BuildWatchlistDigest(_news_factory(engine)).execute(
        BuildWatchlistDigestQuery(
            account_id=ACCOUNT,
            universe=UNIVERSE,
            known_at=known_at,
            published_from=PUBLISHED - timedelta(days=7),
            published_to=known_at,
            max_items=max_items,
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
    return build_snapshot(digest, contexts, account_id=ACCOUNT, generated_at=generated_at)


def _instrument(snapshot: dict[str, Any], symbol: str) -> dict[str, Any]:
    body: Any = snapshot["body"]
    return next(entry for entry in body["instruments"] if entry["canonical_symbol"] == symbol)


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
async def test_repeating_an_export_produces_an_identical_body(
    migrated: AsyncEngine,
) -> None:
    """The contract, over rows the database ordered rather than a dict did."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(
        migrated,
        _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"),
        _bar(SBIN, CUTOFF_DAY, "110"),
    )

    first = await _snapshot(migrated)
    second = await _snapshot(migrated, generated_at=GENERATED + timedelta(hours=3))

    assert serialise_snapshot({"b": first["body"]}) == serialise_snapshot({"b": second["body"]})
    assert first["envelope"]["body_sha256"] == second["envelope"]["body_sha256"]
    assert first["envelope"]["generated_at"] != second["envelope"]["generated_at"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_fingerprint_verifies_against_the_exported_body(
    migrated: AsyncEngine,
) -> None:
    """A reader with the file alone can detect an edit."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    snapshot = await _snapshot(migrated)

    assert body_fingerprint(snapshot["body"]) == snapshot["envelope"]["body_sha256"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_instrument_ordering_is_stable_across_exports(
    migrated: AsyncEngine,
) -> None:
    """Row order from PostgreSQL is not guaranteed; the snapshot's order is."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _ingest(migrated, "hal-order", "Hindustan Aeronautics bags a large defence order")

    orders = []
    for _ in range(3):
        body: Any = (await _snapshot(migrated))["body"]
        orders.append([entry["canonical_symbol"] for entry in body["instruments"]])

    assert orders[0] == orders[1] == orders[2]


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_snapshot_round_trips_through_json(migrated: AsyncEngine) -> None:
    """A file that cannot be re-read is not an export."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    text = serialise_snapshot(await _snapshot(migrated))

    assert json.loads(text)["body"]["schema_version"] == EXPORT_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# The cutoff is absolute, in both archives
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_news_first_seen_after_the_cutoff_is_absent(migrated: AsyncEngine) -> None:
    """A snapshot claiming knowledge DHRUVA did not have would be worthless."""
    await _ingest(migrated, "sbi-audit", FRAUD, first_seen_at=LATE_SEEN, analysed_at=LATE_ANALYSED)

    at_cutoff = _instrument(await _snapshot(migrated), "SBIN")
    later = _instrument(await _snapshot(migrated, known_at=LATER), "SBIN")

    assert at_cutoff["has_news"] is False
    assert later["has_news"] is True


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_bar_retrieved_after_the_cutoff_is_absent(migrated: AsyncEngine) -> None:
    """Trading date earlier, retrieval later: still invisible at the cutoff."""
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY - timedelta(days=1), "100"))
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "500", retrieved_at=LATE_RETRIEVAL))

    at_cutoff = _instrument(await _snapshot(migrated), "SBIN")["market"]
    later = _instrument(await _snapshot(migrated, known_at=LATER), "SBIN")["market"]

    assert at_cutoff["latest_close"] == "100"
    assert later["latest_close"] == "500"


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_correction_appears_only_after_it_was_observed(
    migrated: AsyncEngine,
) -> None:
    """Both revisions are stored; the snapshot exports the knowable one."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _ingest(
        migrated, "sbi-audit", CORRECTED, first_seen_at=LATE_SEEN, analysed_at=LATE_ANALYSED
    )

    at_cutoff = _instrument(await _snapshot(migrated), "SBIN")
    later = _instrument(await _snapshot(migrated, known_at=LATER), "SBIN")

    assert at_cutoff["news"][0]["title"] == FRAUD
    assert later["news"][0]["title"] == CORRECTED
    assert at_cutoff["news"][0]["content_revision"] != later["news"][0]["content_revision"]


# --------------------------------------------------------------------------- #
# Gaps, stated
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_quiet_instrument_with_no_bars_states_both_gaps(
    migrated: AsyncEngine,
) -> None:
    """Two absences, two explicit statements, neither a blank."""
    section = _instrument(await _snapshot(migrated), "HAL")

    assert section["has_news"] is False
    assert section["market"]["availability"] == "NO_DATA"
    assert section["market"]["limitation"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_one_stored_bar_exports_insufficient_history(
    migrated: AsyncEngine,
) -> None:
    """A close with nothing to compare it against says exactly that."""
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "110"))

    market = _instrument(await _snapshot(migrated), "SBIN")["market"]

    assert market["availability"] == "INSUFFICIENT_HISTORY"
    assert market["one_day_change_percent"] is None
    assert market["limitation"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_stale_bars_are_flagged_in_the_export(migrated: AsyncEngine) -> None:
    """Presenting an old close as current is the failure that matters most."""
    old = CUTOFF_DAY - timedelta(days=20)
    await _store_bars(migrated, _bar(SBIN, old - timedelta(days=1), "100"), _bar(SBIN, old, "110"))

    market = _instrument(await _snapshot(migrated), "SBIN")["market"]

    assert market["is_stale"] is True
    assert market["staleness_days"] == 20


@pytest.mark.usefixtures("truncated_after_test")
async def test_market_context_omitted_is_distinguishable_from_absent_data(
    migrated: AsyncEngine,
) -> None:
    """A news-only snapshot must not read as evidence that no prices existed."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    body: Any = (await _snapshot(migrated, with_market=False))["body"]

    assert body["market_context_requested"] is False
    assert _instrument({"body": body}, "SBIN")["market"]["requested"] is False


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_export_is_bounded_per_instrument_and_says_so(
    migrated: AsyncEngine,
) -> None:
    """A bound nobody can see would make a truncated snapshot look complete."""
    for index in range(6):
        await _ingest(migrated, f"sbi-{index}", f"{FRAUD} number {index}")

    section = _instrument(await _snapshot(migrated, max_items=2), "SBIN")

    assert section["items_shown"] == 2
    assert section["items_withheld"] == 4


# --------------------------------------------------------------------------- #
# Attribution and read-only behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_attribution_survives_the_round_trip(migrated: AsyncEngine) -> None:
    """An item that cannot be cited cannot be exported."""
    await _ingest(migrated, "sbi-audit", FRAUD)

    entry = _instrument(await _snapshot(migrated), "SBIN")["news"][0]

    assert entry["source"]["key"] == GDELT_SOURCE_KEY
    assert entry["source"]["attribution_url"] == "https://gdeltproject.org"
    assert entry["url"] == f"https://{PUBLISHER}/sbi-audit"
    assert PUBLISHER in entry["source"]["display_name"]


@pytest.mark.usefixtures("truncated_after_test")
async def test_exporting_writes_nothing_to_the_database(migrated: AsyncEngine) -> None:
    """It is a query. Repeating it must not change the archive."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "110"))
    before = await _counts(migrated)

    for _ in range(3):
        await _snapshot(migrated)

    assert await _counts(migrated) == before


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_exported_file_carries_no_credentials_or_local_paths(
    migrated: AsyncEngine,
) -> None:
    """A snapshot is a file somebody may email; it must be safe to send."""
    await _ingest(migrated, "sbi-audit", FRAUD)
    await _store_bars(migrated, _bar(SBIN, CUTOFF_DAY, "110"))

    text = serialise_snapshot(await _snapshot(migrated)).lower()

    for forbidden in ("password", "secret", "api_key", "access_token", "c:\\", "/home/"):
        assert forbidden not in text
