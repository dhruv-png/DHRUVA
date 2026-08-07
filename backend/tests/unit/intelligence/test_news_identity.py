"""News identity, permitted text and every bounded deduplication rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dhruva.contexts.intelligence.domain.news import (
    MAX_SNIPPET,
    MAX_TITLE,
    MIN_REWRITE_TOKENS,
    NEWS_IDENTITY_REVISION,
    DeduplicationLedger,
    DeduplicationRule,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
    normalise_headline,
    significant_tokens,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import InvariantViolation

pytestmark = pytest.mark.unit

PUBLISHED = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
NEXT_DAY = datetime(2026, 8, 4, 5, 30, tzinfo=UTC)
ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from Ministry"
CORRECTED_ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 4,200 crore from Ministry"

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


def _item(
    source: NewsSource,
    provider_item_id: str,
    url: str,
    title: str,
    *,
    published_at: datetime = PUBLISHED,
) -> NewsItem:
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=source.key,
            provider_item_id=provider_item_id,
            url=canonical_url(url),
        ),
        source=source,
        text=PermittedText(title=title),
        published_at=published_at,
        first_seen_at=published_at + timedelta(minutes=30),
    )


# --------------------------------------------------------------------------- #
# Canonical URLs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://WWW.Example.COM:443/a/b/?utm_source=x&b=2&a=1#frag",
            "https://example.com/a/b?a=1&b=2",
        ),
        ("http://example.com/a/b/", "http://example.com/a/b"),
        ("https://example.com/a?ref=tw&fbclid=z", "https://example.com/a"),
        ("https://example.com/a?id=7", "https://example.com/a?id=7"),
    ],
)
def test_canonical_url_reduces_a_link_to_its_document(raw: str, expected: str) -> None:
    """Case, default ports, fragments and referrer parameters do not name a document."""
    assert canonical_url(raw) == expected


def test_canonical_url_keeps_parameters_it_cannot_prove_are_tracking() -> None:
    """An unknown parameter may select the article; dropping it would lose it."""
    assert canonical_url("https://example.com/x?page=2&utm_medium=rss") == (
        "https://example.com/x?page=2"
    )


@pytest.mark.parametrize("raw", ["", " https://example.com", "ftp://example.com/a", "not-a-url"])
def test_canonical_url_refuses_anything_that_is_not_an_absolute_web_link(raw: str) -> None:
    """A link DHRUVA cannot send a reader to is not attribution."""
    with pytest.raises(ValidationError):
        canonical_url(raw)


def test_normalisation_folds_case_punctuation_and_spacing_only() -> None:
    """Two spellings of one headline fold together; different words do not."""
    assert normalise_headline("HAL  bags order, worth Rs 5,000!") == (
        "hal bags order worth rs 5 000"
    )
    assert significant_tokens("The order and the contract") == ("order", "contract")


# --------------------------------------------------------------------------- #
# Identity and permitted text
# --------------------------------------------------------------------------- #


def test_an_item_must_belong_to_the_source_that_delivered_it() -> None:
    """Attribution is structural, not a field somebody remembers to set."""
    with pytest.raises(InvariantViolation, match="identity and source must agree"):
        NewsItem(
            identity=NewsItemIdentity(
                source_key=OTHER_WIRE.key,
                provider_item_id="1",
                url="https://wire-b.example/1",
            ),
            source=WIRE,
            text=PermittedText(title=ORDER_HEADLINE),
            published_at=PUBLISHED,
            first_seen_at=PUBLISHED,
        )


def test_an_item_cannot_be_observed_before_it_was_published() -> None:
    """The two timestamps are what make a point-in-time read possible (ADR-007)."""
    with pytest.raises(InvariantViolation, match="cannot be observed before"):
        NewsItem(
            identity=NewsItemIdentity(
                source_key=WIRE.key,
                provider_item_id="1",
                url="https://wire-a.example/1",
            ),
            source=WIRE,
            text=PermittedText(title=ORDER_HEADLINE),
            published_at=NEXT_DAY,
            first_seen_at=PUBLISHED,
        )


def test_publication_and_first_seen_times_are_kept_separately() -> None:
    """A correction keeps its publication time, so it cannot leak backwards."""
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    assert item.published_at == PUBLISHED
    assert item.first_seen_at == PUBLISHED + timedelta(minutes=30)
    assert item.first_seen_at > item.published_at


@pytest.mark.parametrize("field", ["published_at", "first_seen_at"])
def test_naive_timestamps_are_refused(field: str) -> None:
    """A timestamp without a zone is not an instant (ADR-006)."""
    times = {"published_at": PUBLISHED, "first_seen_at": PUBLISHED + timedelta(minutes=30)}
    times[field] = times[field].replace(tzinfo=None)

    with pytest.raises(InvariantViolation, match="must be aware"):
        NewsItem(
            identity=NewsItemIdentity(
                source_key=WIRE.key,
                provider_item_id="1",
                url="https://wire-a.example/1",
            ),
            source=WIRE,
            text=PermittedText(title=ORDER_HEADLINE),
            **times,
        )


def test_permitted_text_cannot_hold_an_article_body() -> None:
    """The bound is the licence control; a type that cannot hold a body will not."""
    with pytest.raises(InvariantViolation, match="snippet is too long"):
        PermittedText(title=ORDER_HEADLINE, snippet="x" * (MAX_SNIPPET + 1))
    with pytest.raises(InvariantViolation, match="title is too long"):
        PermittedText(title="x" * (MAX_TITLE + 1))


def test_a_snippet_is_optional_but_a_title_is_not() -> None:
    """Every source gives a headline; not every source permits an extract."""
    assert PermittedText(title=ORDER_HEADLINE).snippet is None
    with pytest.raises(InvariantViolation, match="title must not be empty"):
        PermittedText(title="")


def test_an_identity_requires_an_already_canonical_link() -> None:
    """Normalising inside the type would hide which form was stored."""
    with pytest.raises(InvariantViolation, match="must already be canonical"):
        NewsItemIdentity(
            source_key=WIRE.key,
            provider_item_id="1",
            url="https://wire-a.example/1?utm_source=x",
        )


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #


def test_fingerprints_are_deterministic_and_name_their_revision() -> None:
    """The same item yields the same keys in every process and every run."""
    first = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE).fingerprints
    second = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE).fingerprints

    assert first == second
    assert first.revision == NEWS_IDENTITY_REVISION
    assert len(first.identity) == 64


def test_a_headline_too_short_to_compare_declines_the_rewrite_key() -> None:
    """Absence is the bounded rule refusing to guess, not a match against nothing."""
    short = _item(WIRE, "1", "https://wire-a.example/1", "Q1 results out")
    long = _item(WIRE, "2", "https://wire-a.example/2", ORDER_HEADLINE)

    assert len(significant_tokens(short.text.title)) < MIN_REWRITE_TOKENS
    assert short.fingerprints.rewrite is None
    assert long.fingerprints.rewrite is not None


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


def test_the_same_provider_item_identifier_and_wording_is_a_retry() -> None:
    """A polled feed redelivers; ingestion has to be safe to run as often as it likes."""
    ledger = DeduplicationLedger()
    first = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    assert ledger.observe(first).is_duplicate is False
    decision = ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    assert decision.is_duplicate
    assert decision.rule is DeduplicationRule.PROVIDER_ITEM_ID
    assert decision.original == first.identity


def test_a_known_identifier_carrying_new_wording_is_a_correction() -> None:
    """A publisher editing an article in place must not be filed as a repeat.

    This narrows an earlier rule that treated *any* item bearing a seen
    identifier as a duplicate. That rule made the archive incoherent: a
    correction was stored as its own revision -- which is the whole point of
    keeping revisions -- and simultaneously marked duplicate, so it was never
    analysed and could never appear in a digest, an export or a backtest. The
    correction was in the database and invisible to everything that reads it.

    Narrowed rather than removed: an identical redelivery is still a duplicate,
    which is what keeps a re-poll idempotent.
    """
    ledger = DeduplicationLedger()
    original = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    ledger.observe(original)

    corrected = ledger.observe(
        _item(WIRE, "1", "https://wire-a.example/1", CORRECTED_ORDER_HEADLINE)
    )

    assert corrected.is_duplicate is False
    assert corrected.rule is None
    assert "correction" in corrected.reason


def test_a_correction_is_not_then_caught_by_the_link_rule() -> None:
    """The corrected article is of course still at the same URL.

    The link rule exists to find the *same* story published somewhere else, so
    letting a correction fall through to it would undo the narrowing above.
    """
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    corrected = ledger.observe(
        _item(WIRE, "1", "https://wire-a.example/1", CORRECTED_ORDER_HEADLINE)
    )

    assert corrected.rule is not DeduplicationRule.CANONICAL_URL
    assert corrected.is_duplicate is False


def test_a_second_provider_item_at_one_link_is_still_a_duplicate() -> None:
    """One press release delivered twice under two identifiers is one article."""
    ledger = DeduplicationLedger()
    first = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    ledger.observe(first)

    decision = ledger.observe(
        _item(WIRE, "2", "https://wire-a.example/1", CORRECTED_ORDER_HEADLINE)
    )

    assert decision.is_duplicate
    assert decision.rule is DeduplicationRule.CANONICAL_URL


def test_the_content_hash_moves_only_when_the_stored_wording_moves() -> None:
    """It is what tells a re-poll apart from an edit, so it must track wording."""
    original = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    repolled = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    corrected = _item(WIRE, "1", "https://wire-a.example/1", CORRECTED_ORDER_HEADLINE)

    assert original.fingerprints.content_hash == repolled.fingerprints.content_hash
    assert original.fingerprints.content_hash != corrected.fingerprints.content_hash


def test_the_same_canonical_link_is_one_article_shared_twice() -> None:
    """Tracking parameters make one press release look like eleven headlines."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(WIRE, "2", "https://wire-a.example/1?utm_source=tw", "Another headline entirely")
    )

    assert decision.rule is DeduplicationRule.CANONICAL_URL


def test_an_identical_headline_elsewhere_on_the_same_day_is_syndication() -> None:
    """Case and punctuation differ between a wire and the site that republished it."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(OTHER_WIRE, "9", "https://wire-b.example/9", ORDER_HEADLINE.upper())
    )

    assert decision.rule is DeduplicationRule.SYNDICATED_HEADLINE


def test_the_same_filing_from_the_same_official_source_is_a_repeat() -> None:
    """An exchange feed republishes a filing; attribution stays with the first."""
    ledger = DeduplicationLedger()
    first = _item(FILINGS, "F1", "https://nseindia.com/f/1", "Board Meeting Intimation for Results")

    ledger.observe(first)
    decision = ledger.observe(
        _item(FILINGS, "F2", "https://nseindia.com/f/2", "Board Meeting Intimation for Results")
    )

    assert decision.rule is DeduplicationRule.REPEATED_FILING
    assert decision.original == first.identity


def test_the_same_words_reordered_on_the_same_day_is_a_rewrite() -> None:
    """A desk reorders a wire headline; it is still the one event."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(
            OTHER_WIRE,
            "2",
            "https://wire-b.example/2",
            "Bags order worth Rs 5,000 crore from the Ministry: Hindustan Aeronautics",
        )
    )

    assert decision.rule is DeduplicationRule.REWRITTEN_HEADLINE


def test_a_reworded_headline_is_not_collapsed_by_a_fuzzy_guess() -> None:
    """Set equality catches reordering. It does not pretend to read English."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(
            OTHER_WIRE,
            "2",
            "https://wire-b.example/2",
            "Hindustan Aeronautics bagged an order worth Rs 5,000 crore from Ministry",
        )
    )

    assert decision.is_duplicate is False


def test_a_different_number_is_a_different_event() -> None:
    """The failure fuzzy matching produces is two contracts becoming one."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(
            OTHER_WIRE,
            "2",
            "https://wire-b.example/2",
            "Hindustan Aeronautics bags order worth Rs 9,000 crore from Ministry",
        )
    )

    assert decision.is_duplicate is False


def test_the_same_headline_on_a_later_day_is_a_new_event() -> None:
    """Two quarters produce the same sentence; they are not the same announcement."""
    ledger = DeduplicationLedger()
    ledger.observe(_item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE))

    decision = ledger.observe(
        _item(OTHER_WIRE, "2", "https://wire-b.example/2", ORDER_HEADLINE, published_at=NEXT_DAY)
    )

    assert decision.is_duplicate is False


def test_every_decision_explains_itself_and_names_its_revision() -> None:
    """A deduplication nobody can check is a deduplication nobody should trust."""
    ledger = DeduplicationLedger()
    first = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)
    decisions = ledger.extend((first, _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)))

    for decision in decisions:
        assert decision.reason
        assert decision.revision == NEWS_IDENTITY_REVISION
    assert decisions[0].original is None
    assert decisions[1].original == first.identity


def test_observing_an_item_twice_changes_nothing() -> None:
    """Idempotent ingestion: the second poll adds no item and no index entry."""
    ledger = DeduplicationLedger()
    item = _item(WIRE, "1", "https://wire-a.example/1", ORDER_HEADLINE)

    assert ledger.observe(item).is_duplicate is False
    assert ledger.observe(item).is_duplicate is True
    assert ledger.observe(item).rule is DeduplicationRule.PROVIDER_ITEM_ID
