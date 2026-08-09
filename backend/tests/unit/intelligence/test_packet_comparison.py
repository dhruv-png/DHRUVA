"""Packet comparison reads two files, validates each strictly, and compares mechanically.

Two kinds of fixture are used deliberately. Realistic packets -- built
through the real ``build_packet``/``rank_watchlist``/``build_digest``
pipeline, exactly as :mod:`test_research_packet.py` builds them -- exercise
the comparison categories against genuine production output. Hand-built raw
JSON dicts exercise the *validator*: a hostile or corrupted file would not
come from ``build_packet`` either, so the strict-reading tests construct
exactly the malformed shapes a real file could arrive as.
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
from dhruva.contexts.intelligence.interfaces.digest_export import (
    body_fingerprint,
    serialise_snapshot,
)
from dhruva.contexts.intelligence.interfaces.packet_comparison import (
    COMPARISON_SCHEMA_VERSION,
    PacketChangeCategory,
    build_comparison_export,
    compare_packets,
    read_packet,
    render_packet_comparison,
    validate_packet_pair,
)
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
    summarise_recent_bars,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
OTHER_ACCOUNT: Final = AccountId.deterministic("owner-other")
CUTOFF_DAY: Final = date(2026, 8, 3)
PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
LATER_ANALYSED: Final = ANALYSED + timedelta(days=1)
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
COCHINSHIP: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "COCHINSHIP"),
    canonical_symbol="COCHINSHIP",
    company_name="Cochin Shipyard Limited",
)
UNIVERSE: Final = (SBIN, HAL, COCHINSHIP)


def _archived(url: str, title: str, *, first_seen_at: datetime = SEEN) -> ArchivedNewsItem:
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
        first_seen_at=first_seen_at,
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
            analysed_at=max(first_seen_at, ANALYSED),
        ),
    )


def _series(instrument: LinkableInstrument, *, closes: tuple[str, ...]) -> DailyBarSeries:
    bars = []
    for index, close in enumerate(closes):
        price = Decimal(close)
        bars.append(
            DailyBarRevision(
                instrument_id=instrument.instrument_id,
                instrument_kind=MarketInstrumentKind.CASH_EQUITY,
                source="kite",
                source_instrument_id=1,
                candle=DailyCandle(
                    trading_date=CUTOFF_DAY - timedelta(days=len(closes) - 1 - index),
                    open=price,
                    high=price + 1,
                    low=price - 1,
                    close=price,
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


def _moved_context(instrument: LinkableInstrument, *, closes: tuple[str, ...]) -> MarketContext:
    """Build a genuine, deterministic price move; a flat close scores zero attention."""
    return summarise_recent_bars(_series(instrument, closes=closes), as_of=CUTOFF_DAY)


def _context_at(
    instrument: LinkableInstrument, *, closes: tuple[str, ...], as_of: date
) -> MarketContext:
    """Summarise the identical bars as of a caller-chosen cutoff.

    Lets a test hold every stored market fact fixed while moving only the
    cutoff -- exactly what a later re-export of an unchanged instrument does
    in production, where ``as_of`` and ``staleness_days`` advance with the
    calendar even though no new bar was ever ingested.
    """
    return summarise_recent_bars(_series(instrument, closes=closes), as_of=as_of)


def _digest(*items: ArchivedNewsItem, known_at: datetime = ANALYSED) -> Any:
    return build_digest(
        UNIVERSE,
        items,
        known_at=known_at,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=known_at,
    )


def _packet_dict(
    *items: ArchivedNewsItem,
    contexts: dict[InstrumentId, MarketContext] | None = None,
    top: int = 5,
    known_at: datetime = ANALYSED,
    generated_at: datetime = GENERATED,
    account_id: AccountId = ACCOUNT,
) -> dict[str, Any]:
    digest = _digest(*items, known_at=known_at)
    ranked = rank_watchlist(digest, contexts)
    selected = top_attention(ranked, limit=top)
    return build_packet(
        selected,
        ranked=ranked,
        digest=digest,
        contexts=contexts,
        account_id=account_id,
        generated_at=generated_at,
        requested_top=top,
    )


def _packet_bytes(*args: Any, **kwargs: Any) -> bytes:
    return serialise_snapshot(_packet_dict(*args, **kwargs)).encode("utf-8")


# --------------------------------------------------------------------------- #
# Reading and validating a real, well-formed packet
# --------------------------------------------------------------------------- #


def test_a_genuine_packet_round_trips_through_the_reader() -> None:
    """The reader must accept exactly what the real writer produces."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    data = _packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts)

    packet = read_packet(data)

    assert packet.account_id == str(ACCOUNT)
    assert packet.as_of == ANALYSED
    assert "SBIN" in packet.attention_by_symbol


def test_the_reader_refuses_invalid_utf8() -> None:
    """A non-UTF-8 file is refused before any JSON parsing is attempted."""
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        read_packet(b"\xff\xfe\x00\x01")


def test_the_reader_refuses_invalid_json() -> None:
    """Malformed JSON is refused with a diagnostic, not an uncaught exception."""
    with pytest.raises(ValidationError, match="not valid JSON"):
        read_packet(b"{not json")


def test_the_reader_refuses_a_json_array_at_the_top_level() -> None:
    """A packet is always an object with envelope/body, never a bare array."""
    with pytest.raises(ValidationError, match="JSON object"):
        read_packet(b"[]")


def test_the_reader_refuses_a_missing_envelope_or_body() -> None:
    """Both halves of the envelope/body split are required, not just one."""
    with pytest.raises(ValidationError, match="envelope or body"):
        read_packet(json.dumps({"body": {}}).encode())
    with pytest.raises(ValidationError, match="envelope or body"):
        read_packet(json.dumps({"envelope": {}}).encode())


def test_the_reader_refuses_the_wrong_schema_version() -> None:
    """A full snapshot, or an unknown schema, must not be misread as a packet."""
    raw = json.loads(_packet_bytes())
    raw["body"]["schema_version"] = "dhruva.research-snapshot.v1"

    with pytest.raises(ValidationError, match="unexpected schema_version"):
        read_packet(json.dumps(raw).encode())


def test_the_reader_refuses_a_body_sha_mismatch() -> None:
    """A hand-edited file must be caught, not silently trusted."""
    raw = json.loads(_packet_bytes(_archived("https://p.test/sbi", FRAUD)))
    raw["body"]["top_n"] = 999  # edited after export, fingerprint now stale

    with pytest.raises(ValidationError, match="does not match its own recorded fingerprint"):
        read_packet(json.dumps(raw).encode())


@pytest.mark.parametrize(
    "field",
    ["account_id", "as_of", "top_n", "total_ranked", "watchlist_summary", "attention"],
)
def test_the_reader_refuses_a_missing_required_body_field(field: str) -> None:
    """Every field the comparison depends on must be present, not defaulted."""
    raw = json.loads(_packet_bytes())
    del raw["body"][field]
    raw["envelope"]["body_sha256"] = body_fingerprint(raw["body"])

    with pytest.raises(ValidationError):
        read_packet(json.dumps(raw).encode())


def test_the_reader_refuses_a_missing_attention_revision() -> None:
    """The scoring ruleset identity is required so a stale-model comparison is refused loudly."""
    raw = json.loads(_packet_bytes())
    del raw["body"]["revisions"]["attention"]
    raw["envelope"]["body_sha256"] = body_fingerprint(raw["body"])

    with pytest.raises(ValidationError, match="attention model revision"):
        read_packet(json.dumps(raw).encode())


def test_the_reader_refuses_a_duplicate_instrument_in_the_attention_array() -> None:
    """Two entries for one symbol would make "before"/"after" lookup ambiguous."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    raw = json.loads(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    raw["body"]["attention"].append(raw["body"]["attention"][0])
    raw["envelope"]["body_sha256"] = body_fingerprint(raw["body"])

    with pytest.raises(ValidationError, match="same instrument twice"):
        read_packet(json.dumps(raw).encode())


def test_the_reader_refuses_a_news_entry_without_a_provider_item_id() -> None:
    """News identity is required so added/removed items can be told apart at all."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    raw = json.loads(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    raw["body"]["attention"][0]["news"][0]["provider_item_id"] = ""
    raw["envelope"]["body_sha256"] = body_fingerprint(raw["body"])

    with pytest.raises(ValidationError, match="provider_item_id"):
        read_packet(json.dumps(raw).encode())


# --------------------------------------------------------------------------- #
# Cross-packet validation
# --------------------------------------------------------------------------- #


def test_different_accounts_are_refused() -> None:
    """Comparing two accounts' research states would answer a meaningless question."""
    before = read_packet(_packet_bytes(account_id=ACCOUNT))
    after = read_packet(_packet_bytes(account_id=OTHER_ACCOUNT))

    with pytest.raises(ValidationError, match="different accounts"):
        validate_packet_pair(before, after)


def test_to_earlier_than_from_is_refused() -> None:
    """--from must describe the earlier state; a reversed pair is refused, not silently flipped."""
    before = read_packet(_packet_bytes(known_at=LATER_ANALYSED))
    after = read_packet(_packet_bytes(known_at=ANALYSED))

    with pytest.raises(ValidationError, match="must not be earlier than"):
        validate_packet_pair(before, after)


def test_equal_as_of_is_accepted() -> None:
    """Two packets exported from the same cutoff are a legitimate, if empty, comparison."""
    before = read_packet(_packet_bytes(known_at=ANALYSED, generated_at=GENERATED))
    after = read_packet(
        _packet_bytes(known_at=ANALYSED, generated_at=GENERATED + timedelta(hours=1))
    )

    validate_packet_pair(before, after)  # must not raise


def test_a_later_to_is_accepted() -> None:
    """The ordinary case: --to genuinely describes a later research state."""
    before = read_packet(_packet_bytes(known_at=ANALYSED))
    after = read_packet(_packet_bytes(known_at=LATER_ANALYSED))

    validate_packet_pair(before, after)  # must not raise


# --------------------------------------------------------------------------- #
# Comparison categories
# --------------------------------------------------------------------------- #


def test_identical_packets_produce_no_changes() -> None:
    """Absence of a change is not reported as a change either -- the empty case."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))

    assert compare_packets(before, after) == ()


def test_a_score_increase_is_detected() -> None:
    """A larger 1-day move raises the score and is reported as an increase."""
    before = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "102"))},
        )
    )
    after = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))},
        )
    )

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.SCORE_INCREASED in sbin.categories
    assert sbin.after.score > sbin.before.score  # type: ignore[union-attr]


def test_a_score_decrease_is_detected() -> None:
    """A smaller 1-day move lowers the score and is reported as a decrease."""
    before = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))},
        )
    )
    after = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "102"))},
        )
    )

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.SCORE_DECREASED in sbin.categories


def test_a_band_change_is_detected() -> None:
    """News is deliberately omitted here.

    A notable headline alone is worth 3 points, which would already put both
    sides in ELEVATED and mask a pure move-driven band change; isolating the
    market-only contribution is the point of this test.
    """
    before = read_packet(
        _packet_bytes(contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "102"))})
    )
    after = read_packet(
        _packet_bytes(contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "115"))})
    )

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.BAND_CHANGED in sbin.categories
    assert sbin.before is not None
    assert sbin.after is not None
    assert sbin.before.band != sbin.after.band


def test_reasons_changed_lists_only_the_new_reasons() -> None:
    """Only reasons absent from the earlier packet are reported, not the full new list."""
    before = read_packet(
        _packet_bytes(
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "102"))},
        )
    )
    after = read_packet(
        _packet_bytes(
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))},
        )
    )

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.REASONS_CHANGED in sbin.categories
    assert any("10" in reason for reason in sbin.new_reasons)
    assert "1-day absolute move 2%" not in sbin.new_reasons


def test_market_changed_is_detected_when_the_stored_close_differs() -> None:
    """A different stored close between the two packets is reported even without a band change."""
    before = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))},
        )
    )
    after = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "108"))},
        )
    )

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.MARKET_CHANGED in sbin.categories


def test_market_availability_changing_is_detected() -> None:
    """Going from a resolved MarketContext to none at all is a substantive change.

    News keeps SBIN's score positive on both sides -- present in both
    packets' attention arrays -- so this exercises the "both present" market
    comparison rather than an entry/exit transition.
    """
    before = read_packet(
        _packet_bytes(
            _archived("https://p.test/sbi", FRAUD),
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))},
        )
    )
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=None))

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.MARKET_CHANGED in sbin.categories


def test_the_same_market_facts_at_a_later_cutoff_is_not_reported_as_changed() -> None:
    """Regression: re-exporting an unchanged instrument a day later must not claim MARKET_CHANGED.

    Real owner validation on 2026-08-09 compared a packet as-of 2026-08-08 with
    one as-of 2026-08-09 and saw MARKET_CHANGED on every unchanged instrument,
    purely because the packet's own ``as_of``/``staleness_days`` fields moved
    forward with the calendar. SBIN keeps a genuine, positive-scoring 10% move
    identical on both sides -- present in both packets' attention arrays, so
    the market fingerprint comparison actually runs -- and only the context's
    own ``as_of`` cutoff differs between the two packets.
    """
    before = read_packet(
        _packet_bytes(
            contexts={
                SBIN.instrument_id: _context_at(SBIN, closes=("100", "110"), as_of=CUTOFF_DAY)
            }
        )
    )
    after = read_packet(
        _packet_bytes(
            contexts={
                SBIN.instrument_id: _context_at(
                    SBIN, closes=("100", "110"), as_of=CUTOFF_DAY + timedelta(days=1)
                )
            },
            known_at=LATER_ANALYSED,
        )
    )

    assert "SBIN" in before.attention_by_symbol
    assert "SBIN" in after.attention_by_symbol
    before_market = before.attention_by_symbol["SBIN"].market
    after_market = after.attention_by_symbol["SBIN"].market
    assert before_market["as_of"] != after_market["as_of"]  # the cutoff genuinely differs
    assert before_market["staleness_days"] != after_market["staleness_days"]
    assert before_market["latest_close"] == after_market["latest_close"]  # but nothing stored did

    assert compare_packets(before, after) == ()


def test_news_added_is_detected_and_carries_the_new_item() -> None:
    """A news item present only in the later packet is reported with its own fields."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(contexts=contexts))
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.NEWS_ADDED in sbin.categories
    assert len(sbin.new_news) == 1
    assert sbin.new_news[0]["title"] == FRAUD


def test_news_removed_is_detected_and_carries_the_removed_item() -> None:
    """Structurally supported even though the archive is normally append-only."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    after = read_packet(_packet_bytes(contexts=contexts))

    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.NEWS_REMOVED in sbin.categories
    assert len(sbin.removed_news) == 1
    assert sbin.removed_news[0]["title"] == FRAUD


# --------------------------------------------------------------------------- #
# News on an entry/exit transition -- never claimed as added/removed
# --------------------------------------------------------------------------- #


def test_entering_attention_via_news_alone_does_not_claim_news_added() -> None:
    """Reproduces the real owner-validation case: entry driven by news alone.

    The earlier packet's array is complete (nothing scored, so an empty array
    already proves every score-positive instrument is accounted for) and SBIN
    is absent from it -- proving its score was 0, never that it had zero
    archived news, which packet v1 does not record for a zero-score
    instrument. The later packet's own SBIN entry, and its news, are still
    fully visible on the returned change.
    """
    before = read_packet(_packet_bytes(top=5))  # nothing scored: a complete, empty array
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), top=5))

    assert before.is_complete is True
    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.ENTERED_ATTENTION in sbin.categories
    assert PacketChangeCategory.NEWS_ADDED not in sbin.categories
    assert sbin.new_news == ()
    # The news is not lost -- it is still on the after side of the change.
    assert sbin.after is not None
    assert len(sbin.after.news_ids) == 1
    assert sbin.after.news_by_id[sbin.after.news_ids[0]]["title"] == FRAUD

    rendered = render_packet_comparison((sbin,), before=before, after=after)
    assert "not claimed as newly added" in rendered
    assert FRAUD in rendered
    assert "new archived news:" not in rendered  # that label is reserved for NEWS_ADDED


def test_entering_a_truncated_selection_with_news_does_not_claim_news_added() -> None:
    """The same non-claim holds for the weaker ENTERED_PACKET_SELECTION case."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110")),
        HAL.instrument_id: _moved_context(HAL, closes=("100", "109")),
        COCHINSHIP.instrument_id: _moved_context(COCHINSHIP, closes=("100", "108")),
    }
    # top=1 makes the "before" array truncated -- incomplete -- even though
    # every instrument here has a positive score.
    before = read_packet(_packet_bytes(contexts=contexts, top=1))
    after = read_packet(
        _packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts, top=3)
    )

    assert before.is_complete is False
    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.ENTERED_PACKET_SELECTION in sbin.categories
    assert PacketChangeCategory.NEWS_ADDED not in sbin.categories
    assert sbin.new_news == ()
    assert sbin.after is not None
    assert len(sbin.after.news_ids) == 1


def test_exiting_attention_with_news_present_does_not_claim_news_removed() -> None:
    """The symmetric case on the way out: news is shown, never claimed as removed."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    after = read_packet(_packet_bytes(top=5))  # SBIN scores 0 and is absent

    assert after.is_complete is True
    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.EXITED_ATTENTION in sbin.categories
    assert PacketChangeCategory.NEWS_REMOVED not in sbin.categories
    assert sbin.removed_news == ()
    assert sbin.before is not None
    assert len(sbin.before.news_ids) == 1

    rendered = render_packet_comparison((sbin,), before=before, after=after)
    assert "not claimed as newly added" in rendered
    assert FRAUD in rendered
    assert "archived news no longer present:" not in rendered  # reserved for NEWS_REMOVED


# --------------------------------------------------------------------------- #
# The top-N limitation -- the most important design constraint
# --------------------------------------------------------------------------- #


def test_entering_a_complete_packets_selection_is_a_true_attention_entry() -> None:
    """Top >= with_attention on the earlier side: absence there proves score was 0."""
    before = read_packet(_packet_bytes(top=5))  # nothing scored: a complete, empty array
    after = read_packet(
        _packet_bytes(
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))}, top=5
        )
    )

    assert before.is_complete is True
    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.ENTERED_ATTENTION in sbin.categories
    assert PacketChangeCategory.ENTERED_PACKET_SELECTION not in sbin.categories


def test_leaving_a_complete_packets_selection_is_a_true_attention_exit() -> None:
    """Top >= with_attention on the later side: absence there proves score fell to 0."""
    before = read_packet(
        _packet_bytes(
            contexts={SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))}, top=5
        )
    )
    after = read_packet(_packet_bytes(top=5))

    assert after.is_complete is True
    changes = compare_packets(before, after)
    sbin = next(c for c in changes if c.canonical_symbol == "SBIN")
    assert PacketChangeCategory.EXITED_ATTENTION in sbin.categories
    assert PacketChangeCategory.LEFT_PACKET_SELECTION not in sbin.categories


def test_entering_a_truncated_packets_selection_is_only_a_selection_entry() -> None:
    """Top < with_attention: absence on the earlier side proves nothing about score."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110")),
        HAL.instrument_id: _moved_context(HAL, closes=("100", "109")),
        COCHINSHIP.instrument_id: _moved_context(COCHINSHIP, closes=("100", "108")),
    }
    # top=1: only one instrument fits, so the "before" array is truncated --
    # incomplete -- even though all three instruments have positive scores.
    before = read_packet(_packet_bytes(contexts=contexts, top=1))
    after = read_packet(_packet_bytes(contexts=contexts, top=3))

    assert before.is_complete is False
    changes = compare_packets(before, after)
    # Every instrument absent from `before`'s truncated array must be reported
    # as a *selection* entry, never a true attention entry -- the file cannot
    # prove their score was zero.
    for change in changes:
        if change.before is None:
            assert PacketChangeCategory.ENTERED_PACKET_SELECTION in change.categories
            assert PacketChangeCategory.ENTERED_ATTENTION not in change.categories


def test_leaving_a_truncated_packets_selection_is_only_a_selection_exit() -> None:
    """The core false-claim guard: never say EXITED_ATTENTION when only top-N is provable."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110")),
        HAL.instrument_id: _moved_context(HAL, closes=("100", "109")),
        COCHINSHIP.instrument_id: _moved_context(COCHINSHIP, closes=("100", "108")),
    }
    before = read_packet(_packet_bytes(contexts=contexts, top=3))
    # top=1 on the "after" side: two instruments that may still have a
    # positive score are truncated out, not proven to have scored zero.
    after = read_packet(_packet_bytes(contexts=contexts, top=1))

    assert after.is_complete is False
    changes = compare_packets(before, after)
    for change in changes:
        if change.after is None:
            assert PacketChangeCategory.LEFT_PACKET_SELECTION in change.categories
            assert PacketChangeCategory.EXITED_ATTENTION not in change.categories
            # The rendered text must not claim a score of zero either.
            rendered = render_packet_comparison((change,), before=before, after=after)
            assert "-> LOW 0" not in rendered


# --------------------------------------------------------------------------- #
# Determinism and ordering
# --------------------------------------------------------------------------- #


def test_changes_are_ordered_alphabetically_by_canonical_symbol() -> None:
    """A stable, predictable order regardless of score magnitude or category."""
    contexts = {
        SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110")),
        HAL.instrument_id: _moved_context(HAL, closes=("100", "109")),
        COCHINSHIP.instrument_id: _moved_context(COCHINSHIP, closes=("100", "108")),
    }
    before = read_packet(_packet_bytes(top=5))
    after = read_packet(_packet_bytes(contexts=contexts, top=5))

    changes = compare_packets(before, after)

    symbols = [change.canonical_symbol for change in changes]
    assert symbols == sorted(symbols)


def test_repeated_comparison_is_byte_for_byte_deterministic() -> None:
    """The same two files compared twice must render identically, with no hidden state."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    after = read_packet(
        _packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts, top=3)
    )

    first = render_packet_comparison(compare_packets(before, after), before=before, after=after)
    second = render_packet_comparison(compare_packets(before, after), before=before, after=after)

    assert first == second


# --------------------------------------------------------------------------- #
# Output safety
# --------------------------------------------------------------------------- #


def test_the_disclaimer_is_reused() -> None:
    """The comparison prints the same non-advice disclaimer as the ranked/brief views."""
    before = read_packet(_packet_bytes())
    after = read_packet(_packet_bytes())

    rendered = render_packet_comparison(compare_packets(before, after), before=before, after=after)
    assert ATTENTION_DISCLAIMER in rendered


def test_dhruvas_own_wording_contains_no_advice_language() -> None:
    """The comparison's own authored wording never recommends or evaluates."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))}
    before = read_packet(_packet_bytes(top=5))
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))

    rendered = render_packet_comparison(compare_packets(before, after), before=before, after=after)
    framing = rendered.replace(ATTENTION_DISCLAIMER, "").replace(FRAUD, "").lower()

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
        assert word not in framing, f"the comparison must not say {word!r}"


def test_no_article_body_appears_in_a_rendered_comparison() -> None:
    """Only the fields the packet already carries are shown -- never a fetched article body."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "110"))}
    before = read_packet(_packet_bytes(top=5))
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))

    rendered = render_packet_comparison(compare_packets(before, after), before=before, after=after)

    for forbidden in ("body_text", "full_text", '"content"', '"html"'):
        assert forbidden not in rendered


# --------------------------------------------------------------------------- #
# Optional canonical JSON export
# --------------------------------------------------------------------------- #


def test_the_comparison_export_has_its_own_schema_version() -> None:
    """The comparison is its own artifact, versioned independently of the packet it reads."""
    before = read_packet(_packet_bytes())
    after = read_packet(_packet_bytes())

    export = build_comparison_export(
        compare_packets(before, after), before=before, after=after, generated_at=GENERATED
    )

    assert export["envelope"]["schema_version"] == COMPARISON_SCHEMA_VERSION
    assert export["body"]["schema_version"] == COMPARISON_SCHEMA_VERSION
    assert COMPARISON_SCHEMA_VERSION != PACKET_SCHEMA_VERSION


def test_the_comparison_export_body_is_deterministic_and_generated_at_is_not() -> None:
    """generated_at lives only in the envelope, exactly like every other export in this codebase."""
    contexts = {SBIN.instrument_id: _moved_context(SBIN, closes=("100", "106"))}
    before = read_packet(_packet_bytes(top=5))
    after = read_packet(_packet_bytes(_archived("https://p.test/sbi", FRAUD), contexts=contexts))
    changes = compare_packets(before, after)

    first = build_comparison_export(changes, before=before, after=after, generated_at=GENERATED)
    second = build_comparison_export(
        changes, before=before, after=after, generated_at=GENERATED + timedelta(hours=5)
    )

    assert first["body"] == second["body"]
    assert first["envelope"]["body_sha256"] == second["envelope"]["body_sha256"]
    assert first["envelope"]["generated_at"] != second["envelope"]["generated_at"]


def test_the_comparison_export_fingerprint_is_recomputable() -> None:
    """The recorded fingerprint must be reproducible from the body alone, to detect tampering."""
    before = read_packet(_packet_bytes())
    after = read_packet(_packet_bytes())
    export = build_comparison_export(
        compare_packets(before, after), before=before, after=after, generated_at=GENERATED
    )

    assert body_fingerprint(export["body"]) == export["envelope"]["body_sha256"]


def test_the_comparison_export_contains_no_credential_shaped_keys() -> None:
    """A comparison built from two account-scoped files must not leak a credential-shaped value."""
    before = read_packet(_packet_bytes())
    after = read_packet(_packet_bytes())
    export = build_comparison_export(
        compare_packets(before, after), before=before, after=after, generated_at=GENERATED
    )

    text = serialise_snapshot(export).lower()
    for forbidden in ("password", "secret", "api_key", "access_token", "master_key"):
        assert forbidden not in text
