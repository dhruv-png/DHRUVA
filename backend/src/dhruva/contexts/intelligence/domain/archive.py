"""Point-in-time news archive aggregates: what is stored, and when it was known.

Storage here is append-only and bitemporal in the only way that matters for a
backtest. ``published_at`` is what the publisher claims; ``first_seen_at`` is
when DHRUVA observed it. A correction does not edit the row it corrects -- it
arrives as a new revision with its own ``first_seen_at``, so a query asking what
was knowable at an earlier instant still gets the earlier wording (ADR-007).

An *analysis* is one pass of the three deterministic rulesets -- event category,
sentiment, entity linking -- over one revision. It carries all three revision
strings, so re-running an unchanged ruleset is an idempotent no-op while a
changed one appends a second analysis beside the first rather than replacing it.
Nothing is ever reinterpreted in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import (
    ENTITY_LINKING_REVISION,
    EntityLinkResult,
    EntityMatch,
    MatchState,
)
from dhruva.contexts.intelligence.domain.events import EventClassification
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    NewsItem,
    content_fingerprint,
)
from dhruva.contexts.intelligence.domain.sentiment import SentimentResult
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "ArchivedNewsItem",
    "NewsAnalysis",
    "NewsArchiveWrite",
    "NewsRevision",
    "content_revision",
]

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _utc(value: datetime, *, field: str) -> None:
    """Require a timezone-aware UTC instant (ADR-006)."""
    invariant(value.tzinfo is not None and value.utcoffset() is not None, f"{field} must be aware")
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")


def content_revision(item: NewsItem) -> str:
    """Hash the stored content of one item so a correction is a new revision.

    Delegates to :func:`content_fingerprint`, which lives beside the
    deduplication ledger because that ledger needs the same value: without it,
    an edited article keeps its identifier and its link and is filed as a repeat
    of itself. One implementation, so the row identity and the duplicate verdict
    can never disagree about whether content changed.
    """
    return content_fingerprint(
        url=item.identity.url,
        title=item.text.title,
        snippet=item.text.snippet,
        published_at=item.published_at,
    )


@dataclass(frozen=True, slots=True)
class NewsRevision:
    """One observed version of one item, with the verdict on whether it repeats."""

    item: NewsItem
    revision: str
    deduplication: DeduplicationDecision

    def __post_init__(self) -> None:
        """Require a revision that matches the content it claims to identify."""
        invariant(bool(_HEX_SHA256.fullmatch(self.revision)), "invalid news content revision")
        invariant(
            self.revision == content_revision(self.item),
            "content revision does not match the item it identifies",
        )


@dataclass(frozen=True, slots=True)
class NewsAnalysis:
    """One pass of the deterministic rulesets over one revision."""

    event: EventClassification
    sentiment: SentimentResult
    mapping: EntityLinkResult
    analysed_at: datetime

    def __post_init__(self) -> None:
        """Require a knowable analysis instant and consistent mapping evidence."""
        _utc(self.analysed_at, field="analysed_at")
        invariant(
            self.mapping.revision == ENTITY_LINKING_REVISION,
            "analysis carries a mapping from a different linking revision",
        )

    @property
    def linked(self) -> tuple[tuple[EntityMatch, MatchState], ...]:
        """Pair every linked instrument with the confidence it was linked at.

        Ambiguous candidates are included and marked, because an ambiguity that
        is not stored is an ambiguity nobody can review later.
        """
        confident = tuple((match, MatchState.MATCHED) for match in self.mapping.matches)
        contested = tuple((match, MatchState.AMBIGUOUS) for match in self.mapping.ambiguous)
        return confident + contested


@dataclass(frozen=True, slots=True)
class ArchivedNewsItem:
    """One stored revision and the analysis knowable alongside it."""

    revision: NewsRevision
    analysis: NewsAnalysis | None

    def __post_init__(self) -> None:
        """Refuse an analysis that predates the observation it describes."""
        if self.analysis is not None:
            invariant(
                self.analysis.analysed_at >= self.revision.item.first_seen_at,
                "an analysis cannot precede the observation it describes",
            )


@dataclass(frozen=True, slots=True)
class NewsArchiveWrite:
    """Counts distinguishing appended facts from an idempotent re-poll."""

    revisions_added: int
    revisions_unchanged: int
    analyses_added: int
    links_added: int

    def __post_init__(self) -> None:
        """Repository counts cannot be negative."""
        invariant(self.revisions_added >= 0, "revision count cannot be negative")
        invariant(self.revisions_unchanged >= 0, "unchanged count cannot be negative")
        invariant(self.analyses_added >= 0, "analysis count cannot be negative")
        invariant(self.links_added >= 0, "link count cannot be negative")
