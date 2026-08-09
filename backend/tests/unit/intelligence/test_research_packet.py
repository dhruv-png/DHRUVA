"""A packet is deterministic, self-describing, carries no secret, and never advises.

The determinism contract is the same claim ``test_digest_export.py`` proves for
the full snapshot: given the same already-resolved read models, account, cutoff
and top-N bound, the body is byte-for-byte identical. Everything that cannot be
-- the instant of writing -- lives in the envelope alone.
"""

from __future__ import annotations

import hashlib
import json
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
from dhruva.contexts.intelligence.domain.attention import top_attention
from dhruva.contexts.intelligence.domain.digest import build_digest
from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument, link_entities
from dhruva.contexts.intelligence.domain.events import classify_event
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.contexts.intelligence.interfaces.attention_presentation import (
    ATTENTION_DISCLAIMER,
    rank_watchlist,
)
from dhruva.contexts.intelligence.interfaces.digest_export import serialise_snapshot
from dhruva.contexts.intelligence.interfaces.research_packet import (
    PACKET_SCHEMA_VERSION,
    build_packet,
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
    MarketContext,
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
COMMENTARY: Final = "Hindustan Aeronautics shares trade steady in early market activity"

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
COCHINSHIP: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "COCHINSHIP"),
    canonical_symbol="COCHINSHIP",
    company_name="Cochin Shipyard Limited",
)
UNIVERSE: Final = (SBIN, HAL, COCHINSHIP)


def _archived(url: str, title: str) -> ArchivedNewsItem:
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
    return ArchivedNewsItem(
        revision=NewsRevision(
            item=item,
            revision=content_revision(item),
            deduplication=DeduplicationDecision(
                is_duplicate=False, rule=None, original=None, reason="first observation"
            ),
        ),
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
        close = Decimal(100 + index * 3)
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


def _digest(*items: ArchivedNewsItem, known_at: datetime = ANALYSED) -> Any:
    return build_digest(
        UNIVERSE,
        items,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=known_at,
    )


def _moved_context(instrument: LinkableInstrument, *, bars: int = 6) -> MarketContext:
    """Build a genuine, deterministic price move -- a flat close scores zero attention."""
    return summarise_recent_bars(_series(instrument, bars), as_of=CUTOFF_DAY)


def _packet(
    *items: ArchivedNewsItem,
    contexts: dict[InstrumentId, MarketContext] | None = None,
    top: int = 5,
    known_at: datetime = ANALYSED,
    generated_at: datetime = GENERATED,
) -> dict[str, Any]:
    digest = _digest(*items, known_at=known_at)
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


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_two_packets_of_one_cutoff_have_identical_bodies() -> None:
    """The whole contract: same state, same cutoff, same body -- byte for byte."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN)}
    first = _packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts)
    second = _packet(
        _archived("https://p.test/sbi", FRAUD),
        contexts=contexts,
        generated_at=datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
    )

    assert json.dumps(first["body"], sort_keys=True) == json.dumps(second["body"], sort_keys=True)


def test_only_the_envelope_carries_the_moment_of_writing() -> None:
    """Separated so two packets of one cutoff can be diffed without noise."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN)}
    first = _packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts)
    second = _packet(
        _archived("https://p.test/sbi", FRAUD),
        contexts=contexts,
        generated_at=datetime(2026, 8, 7, 23, 59, tzinfo=UTC),
    )

    assert first["envelope"]["generated_at"] != second["envelope"]["generated_at"]
    assert first["envelope"]["body_sha256"] == second["envelope"]["body_sha256"]


def test_serialisation_is_stable_under_json_sorted_keys() -> None:
    """The same canonical-JSON contract the full snapshot already proves."""
    packet = _packet(
        _archived("https://p.test/sbi", FRAUD), contexts={SBIN.instrument_id: _moved_context(SBIN)}
    )

    assert json.dumps(packet["body"], sort_keys=True) == json.dumps(
        json.loads(json.dumps(packet["body"])), sort_keys=True
    )


def test_a_different_top_n_produces_a_different_body_fingerprint() -> None:
    """The packet's shape depends on --top; the fingerprint must reflect that."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN),
        HAL.instrument_id: _moved_context(HAL),
    }
    top_1 = _packet(contexts=contexts, top=1)
    top_2 = _packet(contexts=contexts, top=2)

    assert top_1["envelope"]["body_sha256"] != top_2["envelope"]["body_sha256"]


# --------------------------------------------------------------------------- #
# Self-description
# --------------------------------------------------------------------------- #


def test_the_schema_version_appears_in_both_envelope_and_body() -> None:
    """A consumer pinning a version can refuse a file it does not understand."""
    packet = _packet(_archived("https://p.test/sbi", FRAUD))

    assert packet["envelope"]["schema_version"] == PACKET_SCHEMA_VERSION
    assert packet["body"]["schema_version"] == PACKET_SCHEMA_VERSION


def test_the_cutoff_and_account_are_recorded() -> None:
    """A packet that cannot say which question it answers is not evidence."""
    body: Any = _packet(_archived("https://p.test/sbi", FRAUD))["body"]

    assert body["as_of"] == ANALYSED.isoformat()
    assert body["account_id"] == str(ACCOUNT)


def test_the_attention_revision_is_recorded_alongside_every_other_ruleset() -> None:
    """Six months later, "which model produced this score?" must be answerable."""
    revisions: Any = _packet(_archived("https://p.test/sbi", FRAUD))["body"]["revisions"]

    for key in (
        "attention",
        "digest",
        "entity_linking",
        "event_classification",
        "market_context",
        "news_identity",
        "sentiment",
    ):
        assert revisions[key], f"{key} revision is missing"


def test_the_top_n_and_total_ranked_are_recorded() -> None:
    """A shorter attention array must be distinguishable from a smaller --top."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN),
        HAL.instrument_id: _moved_context(HAL),
    }
    body: Any = _packet(contexts=contexts, top=1)["body"]

    assert body["top_n"] == 1
    assert body["total_ranked"] == len(UNIVERSE)
    assert len(body["attention"]) == 1


def test_the_envelope_carries_the_notice_and_the_attention_disclaimer() -> None:
    """A file read without the terminal still says what it is and is not."""
    envelope: Any = _packet(_archived("https://p.test/sbi", FRAUD))["envelope"]

    assert "NSE" in envelope["notice"]
    assert envelope["disclaimer"] == ATTENTION_DISCLAIMER


# --------------------------------------------------------------------------- #
# Watchlist summary
# --------------------------------------------------------------------------- #


def test_the_watchlist_summary_counts_the_whole_universe_not_just_top_n() -> None:
    """A --top that truncates the attention array must not shrink the summary."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN),
        HAL.instrument_id: _moved_context(HAL),
    }
    body: Any = _packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts, top=1)["body"]

    summary = body["watchlist_summary"]
    assert summary["instruments"] == len(UNIVERSE)
    assert summary["with_market_context"] == 2
    assert summary["with_archived_news"] == 1
    assert summary["with_attention"] == 2


def test_score_zero_instruments_are_excluded_from_attention_but_counted_in_summary() -> None:
    """top_attention's own policy: excluded from the array, still counted."""
    body: Any = _packet()["body"]

    assert body["watchlist_summary"]["instruments"] == len(UNIVERSE)
    assert body["watchlist_summary"]["with_attention"] == 0
    assert body["attention"] == []


# --------------------------------------------------------------------------- #
# Ranking order and score-zero exclusion
# --------------------------------------------------------------------------- #


def test_attention_entries_preserve_rank_watchlists_own_order() -> None:
    """JSON arrays are ordered; a consumer reads the same ranking the brief showed."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN),
        HAL.instrument_id: _moved_context(HAL, bars=2),
    }
    body: Any = _packet(contexts=contexts, top=5)["body"]

    scores = [entry["score"] for entry in body["attention"]]
    assert scores == sorted(scores, reverse=True)


def test_an_empty_packet_states_no_instrument_was_noteworthy() -> None:
    """A quiet watchlist is a valid, complete, empty answer, not an error."""
    body: Any = _packet()["body"]

    assert body["attention"] == []
    assert body["top_n"] == 5


# --------------------------------------------------------------------------- #
# Attribution and reuse of the full-export shape
# --------------------------------------------------------------------------- #


def test_included_instruments_carry_score_band_and_reasons() -> None:
    """The mechanical why -- score, band, reasons -- travels with the entry."""
    entry = _entry(_packet(_archived("https://p.test/sbi", FRAUD)), "SBIN")

    assert entry["score"] > 0
    assert entry["band"] in {"NORMAL", "ELEVATED", "HIGH"}
    assert entry["reasons"]
    assert any("event class" in reason for reason in entry["reasons"])


def test_included_instruments_carry_market_context_in_the_full_export_shape() -> None:
    """Reused from digest_export, not re-derived: the exact same field set."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN)}
    entry = _entry(
        _packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts),
        "SBIN",
    )

    market = entry["market"]
    assert market["requested"] is True
    assert isinstance(market["latest_close"], str)
    assert "revision" in market


def test_included_instruments_carry_news_with_source_attribution() -> None:
    """The same citation fields the full export already guarantees."""
    entry = _entry(_packet(_archived("https://p.test/sbi", FRAUD)), "SBIN")

    item = entry["news"][0]
    assert item["source"]["key"] == "gdelt"
    assert item["source"]["attribution_url"] == "https://gdeltproject.org"
    assert item["url"] == "https://p.test/sbi"
    assert item["published_at"] == PUBLISHED.isoformat()
    assert item["first_seen_at"] == SEEN.isoformat()


def test_market_context_unavailable_is_explicit_not_fabricated_as_zero() -> None:
    """Absence of market data must not be silently read as a flat, unmoved close."""
    entry = _entry(_packet(_archived("https://p.test/sbi", FRAUD), contexts={}), "SBIN")

    assert entry["market_context_available"] is False
    assert entry["market"] == {"requested": False, "availability": None}


def test_market_context_no_data_is_explicit() -> None:
    """NO_DATA is DHRUVA not knowing, never a computed zero-movement answer."""
    contexts = {
        SBIN.instrument_id: absent_context(
            SBIN.instrument_id, as_of=CUTOFF_DAY, reason="nothing stored"
        )
    }
    entry = _entry(_packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts), "SBIN")

    assert entry["market_context_available"] is False
    assert entry["market"]["availability"] == "NO_DATA"
    assert entry["market"]["limitation"]


# --------------------------------------------------------------------------- #
# What must not be in the file
# --------------------------------------------------------------------------- #


def test_the_packet_contains_no_article_body_or_raw_payload() -> None:
    """DHRUVA stores a headline; the article stays with its publisher."""
    text = serialise_snapshot(_packet(_archived("https://p.test/sbi", FRAUD)))

    for forbidden in ('"raw"', '"payload"', '"body_text"', '"content"', '"html"'):
        assert forbidden not in text


def test_the_packet_contains_no_credential_shaped_keys() -> None:
    """A file somebody may email should contain nothing they would mind sending."""
    text = serialise_snapshot(_packet(_archived("https://p.test/sbi", FRAUD))).lower()

    for forbidden in (
        "password",
        "secret",
        "api_key",
        "access_token",
        "master_key",
        "request_token",
        "totp",
    ):
        assert forbidden not in text


def test_the_packet_contains_no_machine_local_path() -> None:
    """An export describes knowledge, not the laptop it was written on."""
    text = serialise_snapshot(_packet(_archived("https://p.test/sbi", FRAUD)))

    for forbidden in ("C:\\", "/home/", "/Users/", "\\\\"):
        assert forbidden not in text


def test_dhruvas_own_wording_contains_no_advice_language() -> None:
    """Strip the disclaimer (which uses these words in the negative); nothing else may."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN)}
    text = serialise_snapshot(_packet(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    framing = text.replace(ATTENTION_DISCLAIMER, "").lower()

    for word in (
        '"buy"',
        '"sell"',
        " buy ",
        " sell ",
        ' hold "',
        "target price",
        "recommend",
        "outperform",
        "expected return",
        "upside",
        "downside",
        "best stock",
        "conviction",
    ):
        assert word not in framing, f"the packet must not say {word!r}"


# --------------------------------------------------------------------------- #
# Empty watchlist
# --------------------------------------------------------------------------- #


def test_an_empty_watchlist_produces_a_valid_packet() -> None:
    """No instruments is a coherent answer and must still be readable."""
    digest = build_digest(
        (), (), known_at=ANALYSED, published_from=PUBLISHED, published_to=ANALYSED
    )
    ranked = rank_watchlist(digest, {})
    selected = top_attention(ranked, limit=5)
    packet = build_packet(
        selected,
        ranked=ranked,
        digest=digest,
        contexts={},
        account_id=ACCOUNT,
        generated_at=GENERATED,
        requested_top=5,
    )

    assert packet["body"]["attention"] == []
    assert packet["body"]["watchlist_summary"]["instruments"] == 0
