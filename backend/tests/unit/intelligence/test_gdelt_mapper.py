"""The GDELT mapper is pure, deterministic and never guesses at a payload."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dhruva.contexts.intelligence.domain.news import MAX_TITLE, NewsSourceTier
from dhruva.contexts.intelligence.domain.sources import SourceHealth
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_ATTRIBUTION_URL,
    GDELT_MAPPER_REVISION,
    GDELT_SOURCE_KEY,
    MAX_ARTICLES,
    map_artlist,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[2] / "fixtures" / "gdelt"
RETRIEVED = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)


def _payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _sanitized() -> bytes:
    return _payload("artlist_sanitized.json")


# --------------------------------------------------------------------------- #
# The documented happy path
# --------------------------------------------------------------------------- #


def test_a_documented_response_maps_to_usable_items() -> None:
    """Four documented rows become four items, newest last."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.HEALTHY
    assert len(result.items) == 4
    assert result.mapper_revision == GDELT_MAPPER_REVISION
    assert result.content_sha256 == hashlib.sha256(_sanitized()).hexdigest()
    dates = [item.published_at for item in result.items]
    assert dates == sorted(dates)


def test_the_seendate_becomes_the_publication_time_and_is_never_after_retrieval() -> None:
    """``seendate`` is GDELT's observation, so it bounds publication from above."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)

    for item in result.items:
        assert item.published_at.tzinfo is UTC
        assert item.published_at <= RETRIEVED
        assert item.first_seen_at == RETRIEVED
        assert item.first_seen_at >= item.published_at


def test_the_canonical_url_is_stored_not_the_tracked_one() -> None:
    """The first fixture row carries a tracking parameter; the stored link does not."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)
    links = {item.identity.url for item in result.items}

    assert "https://example-business-daily.test/markets/hal-order-win" in links
    assert all("utm_source" not in link for link in links)


def test_only_the_headline_is_stored_and_no_body_is_ever_present() -> None:
    """The response carries no body, and the mapper adds no snippet of its own."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)

    for item in result.items:
        assert item.text.snippet is None
        assert 0 < len(item.text.title) <= MAX_TITLE


# --------------------------------------------------------------------------- #
# Attribution, which GDELT's terms require
# --------------------------------------------------------------------------- #


def test_every_item_carries_the_gdelt_citation_and_link() -> None:
    """GDELT's terms require a citation to the project and a link to its site."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)

    for item in result.items:
        assert item.source.key == GDELT_SOURCE_KEY
        assert "GDELT Project" in item.source.display_name
        assert item.source.homepage_url == GDELT_ATTRIBUTION_URL
        assert item.source.tier is NewsSourceTier.AGGREGATOR


def test_the_originating_publisher_is_named_alongside_gdelt() -> None:
    """Attribution names who published the piece, not only who indexed it."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)
    by_link = {item.identity.url: item for item in result.items}
    item = by_link["https://example-business-daily.test/markets/hal-order-win"]

    assert item.source.display_name == "GDELT Project (via example-business-daily.test)"
    assert item.identity.url.startswith("https://example-business-daily.test/")


def test_a_row_without_a_domain_still_credits_gdelt() -> None:
    """A missing publisher is an absent field, not a reason to drop attribution."""
    result = map_artlist(_payload("artlist_partial_rows.json"), retrieved_at=RETRIEVED)

    assert result.items[0].source.display_name == "GDELT Project (via example-wire.test)"
    assert result.items[0].source.homepage_url == GDELT_ATTRIBUTION_URL


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def test_the_item_identity_is_derived_from_the_url_and_says_so() -> None:
    """GDELT supplies no item id, so the derivation is named rather than hidden."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED)
    item = result.items[0]

    assert item.identity.provider_item_id.startswith("url-sha256:")
    assert item.identity.provider_item_id == (
        f"url-sha256:{hashlib.sha256(item.identity.url.encode()).hexdigest()}"
    )


def test_two_rows_for_one_canonical_url_are_mapped_once() -> None:
    """The provider repeating itself must not become two rows in one batch."""
    result = map_artlist(_payload("artlist_partial_rows.json"), retrieved_at=RETRIEVED)
    identities = [item.identity.provider_item_id for item in result.items]

    assert len(identities) == len(set(identities))
    assert len(result.items) == 1


# --------------------------------------------------------------------------- #
# Every distinct outcome
# --------------------------------------------------------------------------- #


def test_an_empty_article_list_is_an_empty_result_not_a_failure() -> None:
    """The feed answered; it had nothing. That is a different fact from an outage."""
    result = map_artlist(_payload("artlist_empty.json"), retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.EMPTY_RESULT
    assert result.status.succeeded
    assert result.items == ()


def test_a_response_with_no_usable_rows_is_also_an_empty_result() -> None:
    """Rows that all fail row-level validation leave nothing to ingest."""
    payload = json.dumps({"articles": [{"title": "no url or date"}]}).encode()

    result = map_artlist(payload, retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.EMPTY_RESULT


def test_bytes_that_are_not_json_are_a_malformed_payload() -> None:
    """The transport succeeded and the content is unusable; say which."""
    result = map_artlist(_payload("artlist_malformed.txt"), retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.MALFORMED_PAYLOAD
    assert result.items == ()


def test_a_shape_the_mapper_does_not_document_is_refused() -> None:
    """A silent provider format change must stop ingestion, not be reinterpreted."""
    result = map_artlist(_payload("artlist_unsupported_schema.json"), retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.UNSUPPORTED_SCHEMA
    assert "articles" in result.status.reason


def test_more_articles_than_the_documented_maximum_is_refused() -> None:
    """250 is the documented ceiling; more means this is not the API we read."""
    rows = [
        {
            "url": f"https://example-wire.test/a/{index}",
            "title": f"Headline number {index}",
            "seendate": "20260803T060000Z",
        }
        for index in range(MAX_ARTICLES + 1)
    ]

    result = map_artlist(json.dumps({"articles": rows}).encode(), retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.UNSUPPORTED_SCHEMA


def test_a_feed_that_has_stopped_moving_is_stale_not_healthy() -> None:
    """A feed that is up and static looks exactly like a quiet day unless said."""
    result = map_artlist(
        _sanitized(),
        retrieved_at=RETRIEVED + timedelta(days=3),
        fresh_within=timedelta(hours=12),
    )

    assert result.status.health is SourceHealth.STALE
    assert result.items == ()


def test_freshness_is_only_applied_when_the_caller_asks_for_it() -> None:
    """A backfill has no freshness requirement and must not be told it is stale."""
    result = map_artlist(_sanitized(), retrieved_at=RETRIEVED + timedelta(days=3))

    assert result.status.health is SourceHealth.HEALTHY


def test_an_article_seen_after_our_retrieval_instant_is_declined() -> None:
    """Clamping the timestamp would invent one; declining keeps the clash visible."""
    payload = json.dumps(
        {
            "articles": [
                {
                    "url": "https://example-wire.test/future",
                    "title": "An article from the future",
                    "seendate": "20260804T060000Z",
                }
            ]
        }
    ).encode()

    result = map_artlist(payload, retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.EMPTY_RESULT


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_a_result_that_is_not_healthy_never_carries_items() -> None:
    """Every failure path returns nothing to ingest, by construction."""
    for name in (
        "artlist_empty.json",
        "artlist_unsupported_schema.json",
        "artlist_malformed.txt",
    ):
        result = map_artlist(_payload(name), retrieved_at=RETRIEVED)
        assert result.items == ()
        assert result.usable_items == ()


def test_mapping_is_deterministic() -> None:
    """The same bytes map identically in every process and every run."""
    first = map_artlist(_sanitized(), retrieved_at=RETRIEVED)
    second = map_artlist(_sanitized(), retrieved_at=RETRIEVED)

    assert first == second


def test_every_result_names_the_mapper_revision_and_hashes_its_input() -> None:
    """A stored item produced by an older mapper must be visibly older."""
    for name in ("artlist_sanitized.json", "artlist_empty.json", "artlist_malformed.txt"):
        result = map_artlist(_payload(name), retrieved_at=RETRIEVED)
        assert result.mapper_revision == GDELT_MAPPER_REVISION
        assert result.content_sha256 == hashlib.sha256(_payload(name)).hexdigest()


def test_an_over_long_headline_is_truncated_rather_than_dropped() -> None:
    """A long headline is still evidence; the stored bound is what it must fit."""
    payload = json.dumps(
        {
            "articles": [
                {
                    "url": "https://example-wire.test/long",
                    "title": "x" * (MAX_TITLE + 50),
                    "seendate": "20260803T060000Z",
                }
            ]
        }
    ).encode()

    result = map_artlist(payload, retrieved_at=RETRIEVED)

    assert result.status.health is SourceHealth.HEALTHY
    assert len(result.items[0].text.title) == MAX_TITLE
