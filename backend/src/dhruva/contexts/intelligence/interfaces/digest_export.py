"""Serialise a digest and its market context into a self-describing snapshot.

The question this answers is not "what does the digest look like" but "what did
DHRUVA know at this cutoff, and what evidence supported it?" — six months later,
without the database that produced it. So every value carries the provenance
needed to check it: the article's own URL, the source and its citation link, the
timestamps that make the point-in-time claim auditable, and the revision string
of every ruleset that reached a verdict.

**The body is deterministic; the envelope is not.** Given the same database
state, account, cutoff and schema version, ``snapshot["body"]`` is
byte-for-byte identical across runs. ``snapshot["envelope"]`` carries the one
thing that cannot be — the instant the file was written — and is deliberately
kept out of the body so that two exports of the same cutoff can be diffed, and
so ``body_sha256`` is a stable name for a state of knowledge rather than for a
moment of writing.

That split is the whole determinism contract, and it is checkable: the digest
recomputes the hash of the body it was handed, so a snapshot whose body was
edited after export no longer matches the fingerprint it carries.

**Exact values leave as canonical decimal strings**, never as JSON numbers.
JSON's only numeric type is binary floating point, so a close of ``143.50``
could round-trip as ``143.49999999999997``. The canonical form also drops the
storage scale a ``NUMERIC`` column returns, so a value is rendered the same
whether it came from memory or from PostgreSQL -- without which the determinism
claim above would hold only until a value came through the ORM.

**What is deliberately absent.** No raw provider payload, no article body, no
credential, no machine-local path, no account secret. The account appears as its
stable surrogate identifier, which identifies without revealing. A snapshot is a
file somebody may email to themselves; it should contain nothing they would mind
having emailed.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from dhruva.contexts.intelligence.domain.digest import DIGEST_REVISION
from dhruva.contexts.intelligence.domain.entity_linking import ENTITY_LINKING_REVISION
from dhruva.contexts.intelligence.domain.events import EVENT_CLASSIFICATION_REVISION
from dhruva.contexts.intelligence.domain.news import NEWS_IDENTITY_REVISION
from dhruva.contexts.intelligence.domain.sentiment import SENTIMENT_RULESET_REVISION
from dhruva.contexts.intelligence.interfaces.digest_presentation import DIGEST_DISCLAIMER
from dhruva.contexts.intelligence.interfaces.news_presentation import NSE_UNAVAILABLE_NOTICE
from dhruva.contexts.marketdata.api import MARKET_CONTEXT_REVISION
from dhruva.shared.decimals import canonical_decimal_or_none

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.digest import (
        DigestEntry,
        DigestSection,
        WatchlistDigest,
    )
    from dhruva.contexts.marketdata.api import MarketContext
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "body_fingerprint",
    "build_snapshot",
    "serialise_snapshot",
]

#: Bumped whenever the *shape* of the body changes in a way a reader could
#: notice. A consumer that pins this can refuse a file it does not understand
#: instead of silently misreading one.
EXPORT_SCHEMA_VERSION = "dhruva.research-snapshot.v1"

#: Sorted, compact, UTF-8, newline-terminated. Every one of those is load
#: bearing for determinism: unsorted keys differ per run under hash
#: randomisation, and a trailing-space separator differs per json version.
_JSON_ARGUMENTS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "separators": (",", ":"),
}


def build_snapshot(
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
    *,
    account_id: AccountId,
    generated_at: datetime,
) -> dict[str, Any]:
    """Return the complete snapshot: a volatile envelope and a stable body.

    ``generated_at`` reaches only the envelope. Two exports of the same cutoff
    minutes apart differ in exactly one field, which is what makes a diff between
    them meaningful rather than noise.
    """
    body = _body(digest, contexts, account_id=account_id)
    return {
        "envelope": {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "body_sha256": body_fingerprint(body),
            "notice": NSE_UNAVAILABLE_NOTICE,
            "disclaimer": DIGEST_DISCLAIMER,
        },
        "body": body,
    }


def body_fingerprint(body: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the canonical serialisation of ``body``.

    Computed from the same canonical form the file is written in, so a reader
    can recompute it from the file alone and detect an edit without needing the
    database or this code.
    """
    return hashlib.sha256(json.dumps(body, **_JSON_ARGUMENTS).encode("utf-8")).hexdigest()


def serialise_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Return the canonical JSON text of a snapshot, newline-terminated."""
    return json.dumps(snapshot, **_JSON_ARGUMENTS) + "\n"


def _body(
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
    *,
    account_id: AccountId,
) -> dict[str, Any]:
    """Return everything that is a function of the database state alone."""
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "account_id": str(account_id),
        "as_of": digest.known_at.isoformat(),
        "published_from": digest.published_from.isoformat(),
        "published_to": digest.published_to.isoformat(),
        "market_context_requested": contexts is not None,
        "revisions": {
            "digest": digest.revision,
            "digest_policy": DIGEST_REVISION,
            "entity_linking": ENTITY_LINKING_REVISION,
            "event_classification": EVENT_CLASSIFICATION_REVISION,
            "market_context": MARKET_CONTEXT_REVISION,
            "news_identity": NEWS_IDENTITY_REVISION,
            "sentiment": SENTIMENT_RULESET_REVISION,
        },
        "totals": {
            "instruments": len(digest.sections),
            "items_reported": digest.items_reported,
            "instruments_without_news": len(digest.quiet),
        },
        # Section order is the digest's own significance ordering, preserved
        # rather than re-sorted: a consumer reading the list top-down is reading
        # the same ranking an operator saw, and JSON arrays are ordered.
        "instruments": [
            _section(section, None if contexts is None else contexts.get(section.instrument_id))
            for section in digest.sections
        ],
    }


def _section(section: DigestSection, context: MarketContext | None) -> dict[str, Any]:
    """Return one instrument: what it is, what moved, and what was written."""
    return {
        "instrument_id": str(section.instrument_id),
        "canonical_symbol": section.canonical_symbol,
        "company_name": section.company_name,
        "has_news": not section.is_quiet,
        "items_shown": len(section.entries),
        "items_withheld": section.withheld,
        "event_categories": [str(category) for category in section.categories],
        "sentiment_counts": {str(label): count for label, count in section.sentiments},
        "market": _market(context),
        "news": [_entry(entry) for entry in section.entries],
    }


def _market(context: MarketContext | None) -> dict[str, Any]:
    """Return the market summary, or the reason there is not one.

    ``requested`` distinguishes "we did not look" from "we looked and the
    archive was empty". Collapsing the two would let a snapshot taken with
    market context switched off be misread as evidence that no prices existed.
    """
    if context is None:
        return {"requested": False, "availability": None}
    return {
        "requested": True,
        "availability": str(context.availability),
        "as_of": context.as_of.isoformat(),
        "latest_date": None if context.latest_date is None else context.latest_date.isoformat(),
        "latest_close": canonical_decimal_or_none(context.latest_close),
        "previous_close": canonical_decimal_or_none(context.previous_close),
        "one_day_change_percent": canonical_decimal_or_none(context.one_day_change_percent),
        "multi_day_change_percent": canonical_decimal_or_none(context.multi_day_change_percent),
        "multi_day_sessions": context.multi_day_sessions,
        "latest_volume": context.latest_volume,
        "volume_ratio": canonical_decimal_or_none(context.volume_ratio),
        "volume_baseline_sessions": context.volume_baseline_sessions,
        "staleness_days": context.staleness_days,
        "stale_after_days": context.stale_after_days,
        "is_stale": context.is_stale,
        "bars_available": context.bars_available,
        "limitation": context.limitation,
        "revision": context.revision,
    }


def _entry(entry: DigestEntry) -> dict[str, Any]:
    """Return one archived item with everything needed to cite and audit it."""
    news = entry.item.revision.item
    decision = entry.item.revision.deduplication
    analysis = entry.item.analysis
    return {
        "provider_item_id": news.identity.provider_item_id,
        "content_revision": entry.item.revision.revision,
        "title": news.text.title,
        "snippet": news.text.snippet,
        "url": news.identity.url,
        "source": {
            "key": news.source.key,
            "display_name": news.source.display_name,
            "tier": str(news.source.tier),
            "attribution_url": news.source.homepage_url,
        },
        # Both instants, labelled. `published_at` is what the source claims;
        # `first_seen_at` is when DHRUVA observed it, and it is the one a
        # point-in-time audit actually turns on.
        "published_at": news.published_at.isoformat(),
        "first_seen_at": news.first_seen_at.isoformat(),
        "analysed_at": None if analysis is None else analysis.analysed_at.isoformat(),
        "event_category": str(entry.category),
        "is_notable": entry.is_notable,
        "sentiment": str(entry.sentiment),
        "match_state": str(entry.match_state),
        "duplicate": {
            "is_duplicate": decision.is_duplicate,
            "rule": None if decision.rule is None else str(decision.rule),
            "original_url": None if decision.original is None else decision.original.url,
        },
        "linked_instruments": _links(entry),
    }


def _links(entry: DigestEntry) -> list[dict[str, Any]]:
    """Return every instrument the stored analysis linked, ambiguities included."""
    analysis = entry.item.analysis
    if analysis is None:
        return []
    return [
        {
            "instrument_id": str(match.instrument_id),
            "canonical_symbol": match.canonical_symbol,
            "match_kind": str(match.kind),
            "match_state": str(state),
            "matched_text": match.matched_text,
            "relevance": canonical_decimal_or_none(match.relevance),
        }
        for match, state in analysis.linked
    ]
