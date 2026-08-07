"""A snapshot is deterministic, self-describing, and carries no secrets.

The determinism contract is the load-bearing claim: given the same database
state, account, cutoff and schema version, the body is byte-for-byte identical.
Everything that cannot be — the instant of writing — lives in the envelope, and
these tests are what keeps that line from drifting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest

from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.digest import build_digest
from dhruva.contexts.intelligence.domain.entity_linking import (
    LinkableInstrument,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import classify_event
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
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.contexts.intelligence.interfaces.digest_export import (
    EXPORT_SCHEMA_VERSION,
    body_fingerprint,
    build_snapshot,
    serialise_snapshot,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.domain.market_context import (
    absent_context,
    summarise_recent_bars,
)
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
CUTOFF_DAY: Final = date(2026, 8, 3)
PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
GENERATED: Final = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

FRAUD: Final = "State Bank of India faces a forensic audit over accounting irregularities"
ORDER: Final = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project via economictimes.indiatimes.test",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://gdeltproject.org",
)
SBIN: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "SBIN"),
    canonical_symbol="SBIN",
    company_name="State Bank of India",
)
HAL: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "HAL"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
)
UNIVERSE: Final = (SBIN, HAL)


def _archived(url: str, title: str, *, duplicate: bool = False) -> ArchivedNewsItem:
    link = canonical_url(url)
    item = NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT.key,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=GDELT,
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=SEEN,
    )
    decision = (
        DeduplicationDecision(
            is_duplicate=True,
            rule=DeduplicationRule.CANONICAL_URL,
            original=NewsItemIdentity(
                source_key=GDELT.key,
                provider_item_id="url-sha256:" + "0" * 64,
                url=canonical_url("https://p.test/original"),
            ),
            reason="the same canonical link was already stored",
        )
        if duplicate
        else DeduplicationDecision(
            is_duplicate=False, rule=None, original=None, reason="first observation"
        )
    )
    return ArchivedNewsItem(
        revision=NewsRevision(item=item, revision=content_revision(item), deduplication=decision),
        analysis=NewsAnalysis(
            event=classify_event(title),
            sentiment=evaluate_sentiment(title),
            mapping=link_entities(title, universe=UNIVERSE, published_on=PUBLISHED.date()),
            analysed_at=ANALYSED,
        ),
    )


def _series(instrument: LinkableInstrument, count: int, *, last: date = CUTOFF_DAY) -> Any:
    bars = []
    for index in range(count):
        close = Decimal(100 + index)
        bars.append(
            DailyBarRevision(
                instrument_id=instrument.instrument_id,
                instrument_kind=MarketInstrumentKind.CASH_EQUITY,
                source="kite",
                source_instrument_id=1,
                candle=DailyCandle(
                    trading_date=last - timedelta(days=count - 1 - index),
                    open=close,
                    high=close + 1,
                    low=close - 1,
                    close=close,
                    volume=1_000,
                    open_interest=None,
                ),
                retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
                adjustment_status=AdjustmentStatus.RAW,
                completeness=BarCompleteness.COMPLETE,
                source_revision="a" * 64,
                batch_sha256="b" * 64,
                quality_revision="daily-bar-quality-v1",
            )
        )
    return DailyBarSeries(bars=tuple(bars))


def _scaled_series(instrument: LinkableInstrument, count: int, *, scale: int) -> Any:
    """Build the same bars a NUMERIC column would return: values at fixed scale.

    ``Decimal("101").quantize(Decimal("1E-8"))`` is what asyncpg hands back for a
    close written as ``101`` into ``NUMERIC(_, 8)``. Identical number, different
    text -- which is exactly the case the unit suite previously never saw,
    because it only ever built values in memory.
    """
    quantum = Decimal(1).scaleb(-scale)
    original = _series(instrument, count)
    return DailyBarSeries(
        bars=tuple(
            replace(
                bar,
                candle=replace(
                    bar.candle,
                    open=bar.candle.open.quantize(quantum),
                    high=bar.candle.high.quantize(quantum),
                    low=bar.candle.low.quantize(quantum),
                    close=bar.candle.close.quantize(quantum),
                ),
            )
            for bar in original.bars
        )
    )


def _digest(*items: ArchivedNewsItem) -> Any:
    return build_digest(
        UNIVERSE,
        items,
        known_at=ANALYSED,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=ANALYSED,
    )


def _contexts(*, bars: int = 10) -> dict[InstrumentId, Any]:
    return {
        SBIN.instrument_id: summarise_recent_bars(_series(SBIN, bars), as_of=CUTOFF_DAY),
        HAL.instrument_id: absent_context(
            HAL.instrument_id, as_of=CUTOFF_DAY, reason="no complete daily bars stored"
        ),
    }


def _snapshot(*items: ArchivedNewsItem, contexts: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Build a snapshot. ``contexts=False`` means market context was not requested."""
    if contexts is None:
        chosen = _contexts()
    elif contexts is False:
        chosen = None
    else:
        chosen = contexts
    return build_snapshot(
        _digest(*items),
        chosen,
        account_id=ACCOUNT,
        generated_at=kwargs.get("generated_at", GENERATED),
    )


def _instrument(snapshot: dict[str, Any], symbol: str) -> dict[str, Any]:
    body: Any = snapshot["body"]
    return next(entry for entry in body["instruments"] if entry["canonical_symbol"] == symbol)


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_two_exports_of_one_cutoff_have_identical_bodies() -> None:
    """The whole contract: same state, same cutoff, same body -- byte for byte."""
    first = _snapshot(_archived("https://p.test/sbi", FRAUD))
    second = _snapshot(
        _archived("https://p.test/sbi", FRAUD),
        generated_at=datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
    )

    assert json.dumps(first["body"], sort_keys=True) == json.dumps(second["body"], sort_keys=True)


def test_only_the_envelope_carries_the_moment_of_writing() -> None:
    """Separated so two snapshots of one cutoff can be diffed without noise."""
    first = _snapshot(_archived("https://p.test/sbi", FRAUD))
    second = _snapshot(
        _archived("https://p.test/sbi", FRAUD),
        generated_at=datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
    )

    assert first["envelope"]["generated_at"] != second["envelope"]["generated_at"]
    assert first["envelope"]["body_sha256"] == second["envelope"]["body_sha256"]


def test_the_fingerprint_is_recomputable_from_the_body_alone() -> None:
    """A reader with the file and no database can detect an edit."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD))

    assert body_fingerprint(snapshot["body"]) == snapshot["envelope"]["body_sha256"]


def test_an_edited_body_no_longer_matches_its_fingerprint() -> None:
    """That is the point of carrying one."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD))
    tampered = dict(snapshot["body"])
    tampered["as_of"] = "2020-01-01T00:00:00+00:00"

    assert body_fingerprint(tampered) != snapshot["envelope"]["body_sha256"]


def test_serialisation_uses_sorted_keys_so_bytes_are_stable() -> None:
    """Unsorted keys differ between runs under hash randomisation."""
    text = serialise_snapshot(_snapshot(_archived("https://p.test/sbi", FRAUD)))

    assert text.endswith("\n")
    assert text.index('"body"') < text.index('"envelope"')


def test_a_snapshot_round_trips_through_json_unchanged() -> None:
    """A file that cannot be re-read is not an export."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD))
    text = serialise_snapshot(snapshot)

    assert json.loads(text) == json.loads(serialise_snapshot(json.loads(text)))


def test_instrument_order_follows_the_digest_ranking() -> None:
    """JSON arrays are ordered, and a consumer reads the ranking an operator saw."""
    snapshot = _snapshot(
        _archived("https://p.test/hal", ORDER),
        _archived("https://p.test/sbi", FRAUD),
    )
    body: Any = snapshot["body"]

    assert [entry["canonical_symbol"] for entry in body["instruments"]] == ["SBIN", "HAL"]


# --------------------------------------------------------------------------- #
# Self-description
# --------------------------------------------------------------------------- #


def test_the_schema_version_appears_in_both_envelope_and_body() -> None:
    """A consumer pinning a version can refuse a file it does not understand."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD))

    assert snapshot["envelope"]["schema_version"] == EXPORT_SCHEMA_VERSION
    assert snapshot["body"]["schema_version"] == EXPORT_SCHEMA_VERSION


def test_the_cutoff_and_window_are_recorded() -> None:
    """A snapshot that cannot say which question it answers is not evidence."""
    body: Any = _snapshot(_archived("https://p.test/sbi", FRAUD))["body"]

    assert body["as_of"] == ANALYSED.isoformat()
    assert body["published_from"] == (PUBLISHED - timedelta(days=7)).isoformat()


def test_every_ruleset_revision_that_reached_a_verdict_is_recorded() -> None:
    """Six months later, "which classifier said that?" must be answerable."""
    revisions: Any = _snapshot(_archived("https://p.test/sbi", FRAUD))["body"]["revisions"]

    for key in (
        "digest",
        "entity_linking",
        "event_classification",
        "market_context",
        "news_identity",
        "sentiment",
    ):
        assert revisions[key], f"{key} revision is missing"


def test_the_account_appears_as_its_stable_identifier() -> None:
    """It identifies without revealing; a snapshot may be emailed."""
    body: Any = _snapshot(_archived("https://p.test/sbi", FRAUD))["body"]

    assert body["account_id"] == str(ACCOUNT)


def test_the_envelope_carries_the_notice_and_the_disclaimer() -> None:
    """A file read without the terminal still says what it is and is not."""
    envelope: Any = _snapshot(_archived("https://p.test/sbi", FRAUD))["envelope"]

    assert "NSE" in envelope["notice"]
    assert "not advice" in envelope["disclaimer"]


# --------------------------------------------------------------------------- #
# Attribution and auditability
# --------------------------------------------------------------------------- #


def test_every_item_carries_its_source_and_citation_link() -> None:
    """GDELT's terms attach a citation to using the data at all."""
    entry: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "SBIN")["news"][0]

    assert entry["source"]["key"] == "gdelt"
    assert entry["source"]["attribution_url"] == "https://gdeltproject.org"
    assert entry["url"] == "https://p.test/sbi"


def test_both_timestamps_and_the_revision_are_exported() -> None:
    """`first_seen_at` is what a point-in-time audit actually turns on."""
    entry: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "SBIN")["news"][0]

    assert entry["published_at"] == PUBLISHED.isoformat()
    assert entry["first_seen_at"] == SEEN.isoformat()
    assert len(entry["content_revision"]) == 64


def test_a_duplicate_records_the_rule_and_the_item_it_repeats() -> None:
    """The question of why an item is absent must be answerable from the file."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD, duplicate=True))
    entry: Any = _instrument(snapshot, "SBIN")["news"][0]

    assert entry["duplicate"]["is_duplicate"] is True
    assert entry["duplicate"]["rule"] == "CANONICAL_URL"
    assert entry["duplicate"]["original_url"] == "https://p.test/original"


def test_linked_instruments_carry_their_evidence() -> None:
    """The matched text and match kind are why the link exists."""
    entry: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "SBIN")["news"][0]
    link = entry["linked_instruments"][0]

    assert link["canonical_symbol"] == "SBIN"
    assert link["matched_text"]
    assert link["match_kind"]


def test_exact_values_are_exported_as_strings_not_floats() -> None:
    """JSON's only number is binary floating point, and a close is not."""
    market: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "SBIN")["market"]

    assert isinstance(market["latest_close"], str)
    assert isinstance(market["one_day_change_percent"], str)
    assert isinstance(market["latest_volume"], int)


# --------------------------------------------------------------------------- #
# Missing data is stated, never implied
# --------------------------------------------------------------------------- #


def test_a_quiet_instrument_is_exported_with_no_news_and_says_so() -> None:
    """A missing entry is indistinguishable from a lost one."""
    section: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "HAL")

    assert section["has_news"] is False
    assert section["news"] == []
    assert section["items_shown"] == 0


def test_an_instrument_with_no_bars_reports_the_reason() -> None:
    """Not zero, not flat -- absent, with an explanation."""
    market: Any = _instrument(_snapshot(_archived("https://p.test/sbi", FRAUD)), "HAL")["market"]

    assert market["requested"] is True
    assert market["availability"] == "NO_DATA"
    assert market["limitation"]


def test_market_context_switched_off_is_distinguishable_from_absent_data() -> None:
    """Otherwise a news-only snapshot reads as evidence that no prices existed."""
    snapshot = _snapshot(_archived("https://p.test/sbi", FRAUD), contexts=False)
    body: Any = snapshot["body"]

    assert body["market_context_requested"] is False
    assert _instrument(snapshot, "SBIN")["market"] == {"requested": False, "availability": None}


def test_insufficient_history_is_exported_with_its_reason() -> None:
    """A close with nothing to compare it against says exactly that."""
    contexts = {
        SBIN.instrument_id: summarise_recent_bars(_series(SBIN, 1), as_of=CUTOFF_DAY),
    }
    market: Any = _instrument(
        _snapshot(_archived("https://p.test/sbi", FRAUD), contexts=contexts), "SBIN"
    )["market"]

    assert market["availability"] == "INSUFFICIENT_HISTORY"
    assert market["one_day_change_percent"] is None
    assert market["limitation"]


def test_stale_data_is_flagged_independently_of_history() -> None:
    """A series can be both too short to compare and too old to trust."""
    contexts = {
        SBIN.instrument_id: summarise_recent_bars(
            _series(SBIN, 1, last=CUTOFF_DAY - timedelta(days=30)), as_of=CUTOFF_DAY
        ),
    }
    market: Any = _instrument(
        _snapshot(_archived("https://p.test/sbi", FRAUD), contexts=contexts), "SBIN"
    )["market"]

    assert market["is_stale"] is True
    assert market["availability"] == "INSUFFICIENT_HISTORY"
    assert market["staleness_days"] == 30


def test_withheld_items_are_counted_so_truncation_is_visible() -> None:
    """A truncated section must not be readable as a complete one."""
    items = [_archived(f"https://p.test/sbi-{n}", f"{FRAUD} number {n}") for n in range(8)]
    digest = build_digest(
        UNIVERSE,
        items,
        known_at=ANALYSED,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=ANALYSED,
        max_items=3,
    )
    snapshot = build_snapshot(digest, _contexts(), account_id=ACCOUNT, generated_at=GENERATED)

    assert _instrument(snapshot, "SBIN")["items_shown"] == 3
    assert _instrument(snapshot, "SBIN")["items_withheld"] == 5


# --------------------------------------------------------------------------- #
# What must not be in the file
# --------------------------------------------------------------------------- #


def test_the_snapshot_contains_no_article_body_or_raw_payload() -> None:
    """DHRUVA stores a headline; the article stays with its publisher."""
    text = serialise_snapshot(_snapshot(_archived("https://p.test/sbi", FRAUD)))

    for forbidden in ('"raw"', '"payload"', '"body_text"', '"content"', '"html"'):
        assert forbidden not in text


def test_the_snapshot_contains_no_credential_shaped_keys() -> None:
    """A file somebody may email should contain nothing they would mind sending."""
    text = serialise_snapshot(_snapshot(_archived("https://p.test/sbi", FRAUD))).lower()

    for forbidden in ("password", "secret", "api_key", "access_token", "totp"):
        assert forbidden not in text


def test_the_snapshot_contains_no_machine_local_path() -> None:
    """An export describes knowledge, not the laptop it was written on."""
    text = serialise_snapshot(_snapshot(_archived("https://p.test/sbi", FRAUD)))

    for forbidden in ("C:\\", "/home/", "/Users/", "\\\\"):
        assert forbidden not in text


def test_an_empty_watchlist_exports_a_valid_snapshot() -> None:
    """No instruments is a coherent answer and must still be readable."""
    digest = build_digest(
        (), (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED
    )
    snapshot = build_snapshot(digest, {}, account_id=ACCOUNT, generated_at=GENERATED)

    assert snapshot["body"]["instruments"] == []
    assert json.loads(serialise_snapshot(snapshot))["body"]["totals"]["instruments"] == 0


# --------------------------------------------------------------------------- #
# Regressions
# --------------------------------------------------------------------------- #


def test_a_close_carrying_database_scale_renders_canonically() -> None:
    """The defect: NUMERIC(_, 8) returns "100.00000000" for a close of 100.

    Serialising with ``str`` made the output depend on whether a value had been
    through PostgreSQL, which is the one thing a determinism claim cannot
    tolerate. Both spellings must now produce identical JSON.
    """
    from_memory = summarise_recent_bars(_series(SBIN, 2), as_of=CUTOFF_DAY)
    from_database = summarise_recent_bars(_scaled_series(SBIN, 2, scale=8), as_of=CUTOFF_DAY)

    memory_market: Any = _instrument(
        _snapshot(
            _archived("https://p.test/sbi", FRAUD), contexts={SBIN.instrument_id: from_memory}
        ),
        "SBIN",
    )["market"]
    database_market: Any = _instrument(
        _snapshot(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: from_database},
        ),
        "SBIN",
    )["market"]

    assert memory_market["latest_close"] == database_market["latest_close"] == "101"
    assert memory_market == database_market


def test_a_scaled_close_produces_the_same_body_fingerprint() -> None:
    """The fingerprint is the determinism claim in one value; it must agree too."""
    plain = _snapshot(
        _archived("https://p.test/sbi", FRAUD),
        contexts={SBIN.instrument_id: summarise_recent_bars(_series(SBIN, 2), as_of=CUTOFF_DAY)},
    )
    scaled = _snapshot(
        _archived("https://p.test/sbi", FRAUD),
        contexts={
            SBIN.instrument_id: summarise_recent_bars(
                _scaled_series(SBIN, 2, scale=8), as_of=CUTOFF_DAY
            )
        },
    )

    assert plain["envelope"]["body_sha256"] == scaled["envelope"]["body_sha256"]


def test_no_exported_decimal_carries_trailing_zeros() -> None:
    """A snapshot full of "100.00000000" is exact and unreadable in equal measure."""
    market: Any = _instrument(
        _snapshot(
            _archived("https://p.test/sbi", FRAUD),
            contexts={
                SBIN.instrument_id: summarise_recent_bars(
                    _scaled_series(SBIN, 10, scale=8), as_of=CUTOFF_DAY
                )
            },
        ),
        "SBIN",
    )["market"]

    for key in ("latest_close", "previous_close", "one_day_change_percent"):
        value = market[key]
        assert value is not None
        assert not (value.endswith("0") and "." in value), f"{key} kept its storage scale"
