"""Serialise a top-N research-attention selection into a reproducible packet.

Answers a narrower question than :mod:`digest_export` does: not "what did
DHRUVA archive about the whole watchlist" but "which instruments were
noteworthy at this cutoff, and why" -- the same question
:func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.render_brief`
answers as text, exported as the same canonical, self-describing JSON shape
:mod:`digest_export` already established.

**Reused, not reinvented.** The envelope/body split, the ``body_sha256``
determinism contract, the canonical-decimal serialisation, and the exact
per-instrument market and news shape are all :mod:`digest_export`'s own
functions (:func:`~dhruva.contexts.intelligence.interfaces.digest_export.
body_fingerprint`, :func:`~dhruva.contexts.intelligence.interfaces.
digest_export.serialise_snapshot`, :func:`~dhruva.contexts.intelligence.
interfaces.digest_export.market_summary`, :func:`~dhruva.contexts.
intelligence.interfaces.digest_export.news_entry`), imported rather than
copied. This module's own job is only the parts a top-N attention packet adds
that a full-watchlist snapshot does not: the watchlist-wide summary counts,
and one entry per selected instrument carrying its score, band and reasons
alongside the same market/news detail the full export already knows how to
shape.

**No ranking happens here.** ``top`` and ``ranked`` are expected to already be
:func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.
rank_watchlist`'s and :func:`~dhruva.contexts.intelligence.domain.attention.
top_attention`'s output -- this module reads only what its caller already
resolved point-in-time, exactly as :func:`~dhruva.contexts.intelligence.
interfaces.digest_export.build_snapshot` reads an already-built
``WatchlistDigest``. Doing the ranking a second time in two places is how the
two eventually disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dhruva.contexts.intelligence.domain.attention import ATTENTION_REVISION
from dhruva.contexts.intelligence.domain.digest import DIGEST_REVISION
from dhruva.contexts.intelligence.domain.entity_linking import ENTITY_LINKING_REVISION
from dhruva.contexts.intelligence.domain.events import EVENT_CLASSIFICATION_REVISION
from dhruva.contexts.intelligence.domain.news import NEWS_IDENTITY_REVISION
from dhruva.contexts.intelligence.domain.sentiment import SENTIMENT_RULESET_REVISION
from dhruva.contexts.intelligence.interfaces.attention_presentation import ATTENTION_DISCLAIMER
from dhruva.contexts.intelligence.interfaces.digest_export import (
    body_fingerprint,
    market_summary,
    news_entry,
)
from dhruva.contexts.intelligence.interfaces.news_presentation import NSE_UNAVAILABLE_NOTICE
from dhruva.contexts.marketdata.api import MARKET_CONTEXT_REVISION

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.attention import ResearchAttention
    from dhruva.contexts.intelligence.domain.digest import DigestSection, WatchlistDigest
    from dhruva.contexts.marketdata.api import MarketContext
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = ["PACKET_SCHEMA_VERSION", "build_packet"]

#: Independent of :data:`~dhruva.contexts.intelligence.interfaces.
#: digest_export.EXPORT_SCHEMA_VERSION` -- a packet is a different shape (top-N
#: attention selection, not the whole watchlist) and must be able to change
#: without a full-snapshot consumer noticing or a packet consumer accidentally
#: reading a full snapshot as one.
PACKET_SCHEMA_VERSION = "dhruva.research-packet.v1"


def build_packet(  # noqa: PLR0913 - one collaborator per already-resolved read model
    top: Sequence[ResearchAttention],
    *,
    ranked: Sequence[ResearchAttention],
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
    account_id: AccountId,
    generated_at: datetime,
    requested_top: int,
) -> dict[str, Any]:
    """Return the complete packet: a volatile envelope and a stable body.

    ``generated_at`` reaches only the envelope, exactly as
    :func:`~dhruva.contexts.intelligence.interfaces.digest_export.
    build_snapshot` keeps it out of the body. Unlike the full snapshot,
    though, a packet's whole-file byte-determinism is a stated contract of
    ``dhruva.research-packet.v1``: pass ``digest.known_at`` (the resolved PIT
    cutoff, already threaded through this same call as part of ``digest``),
    never a wall-clock read, so that two packets built from identical
    persisted state, account and cutoff serialise to identical bytes --
    envelope included, not merely an identical ``body_sha256``. This function
    does not enforce that itself (``generated_at`` stays an explicit
    parameter, matching every sibling export function in this codebase, and
    matching ADR-011's "time is injected" rule at the one place it is
    injected from); the composition root that calls it is what must uphold
    it. See ``dhruva-export``'s own docstring for where that happens.

    ``requested_top`` is recorded rather than re-derived from ``len(top)``:
    the packet's own shape depends on it (a caller comparing two packets
    needs to know whether a shorter ``attention`` array means fewer
    instruments were noteworthy or a smaller ``--top`` was asked for), and
    only the caller that resolved ``top`` actually knows which is true.
    """
    body = _body(
        top,
        ranked=ranked,
        digest=digest,
        contexts=contexts,
        account_id=account_id,
        requested_top=requested_top,
    )
    return {
        "envelope": {
            "schema_version": PACKET_SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "body_sha256": body_fingerprint(body),
            "notice": NSE_UNAVAILABLE_NOTICE,
            "disclaimer": ATTENTION_DISCLAIMER,
        },
        "body": body,
    }


def _body(  # noqa: PLR0913 - one collaborator per already-resolved read model
    top: Sequence[ResearchAttention],
    *,
    ranked: Sequence[ResearchAttention],
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
    account_id: AccountId,
    requested_top: int,
) -> dict[str, Any]:
    """Return everything that is a function of the database state alone."""
    sections = {section.instrument_id: section for section in digest.sections}
    return {
        "schema_version": PACKET_SCHEMA_VERSION,
        "account_id": str(account_id),
        "as_of": digest.known_at.isoformat(),
        "market_context_requested": contexts is not None,
        "top_n": requested_top,
        "total_ranked": len(ranked),
        "revisions": {
            "attention": ATTENTION_REVISION,
            "digest": digest.revision,
            "digest_policy": DIGEST_REVISION,
            "entity_linking": ENTITY_LINKING_REVISION,
            "event_classification": EVENT_CLASSIFICATION_REVISION,
            "market_context": MARKET_CONTEXT_REVISION,
            "news_identity": NEWS_IDENTITY_REVISION,
            "sentiment": SENTIMENT_RULESET_REVISION,
        },
        "watchlist_summary": _watchlist_summary(ranked, digest=digest),
        # Already ordered by rank_watchlist/top_attention (score, then
        # canonical symbol); preserved rather than re-sorted, for the same
        # reason digest_export keeps the digest's own section order.
        "attention": [
            _attention_entry(
                entry,
                section=sections.get(entry.instrument_id),
                context=None if contexts is None else contexts.get(entry.instrument_id),
            )
            for entry in top
        ],
    }


def _watchlist_summary(
    ranked: Sequence[ResearchAttention], *, digest: WatchlistDigest
) -> dict[str, int]:
    """Return the whole-watchlist counts a reader scans before any single entry.

    Computed over ``ranked`` (every instrument, not just ``top``) and over
    ``digest.sections`` directly, so a --top that truncates the ``attention``
    array below never shrinks what this summary reports.
    """
    return {
        "instruments": len(ranked),
        "with_market_context": sum(1 for entry in ranked if entry.market_context_available),
        "with_archived_news": sum(1 for section in digest.sections if not section.is_quiet),
        "with_attention": sum(1 for entry in ranked if entry.score > 0),
    }


def _attention_entry(
    entry: ResearchAttention, *, section: DigestSection | None, context: MarketContext | None
) -> dict[str, Any]:
    """Return one selected instrument: its verdict, then the market and news behind it."""
    return {
        "instrument_id": str(entry.instrument_id),
        "canonical_symbol": entry.canonical_symbol,
        "company_name": entry.company_name,
        "score": entry.score,
        "band": str(entry.band),
        "reasons": list(entry.reasons),
        "market_context_available": entry.market_context_available,
        "market": market_summary(context),
        "has_news": section is not None and not section.is_quiet,
        "items_shown": 0 if section is None else len(section.entries),
        "items_withheld": 0 if section is None else section.withheld,
        "news": [] if section is None else [news_entry(item) for item in section.entries],
    }
