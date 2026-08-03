"""Domain layer.

Entities, value objects, domain services, domain events and PORTS.

This layer performs no I/O and imports no framework. It may import only
``dhruva.shared`` and the standard library. Enforced by ADR-001 layering
and the import-linter contract ``Clean Architecture layers``.
"""

from dhruva.contexts.intelligence.domain.entity_linking import (
    ENTITY_LINKING_REVISION,
    FORMER_NAME_GRACE,
    EntityLinkResult,
    EntityMatch,
    HistoricalName,
    LinkableInstrument,
    MatchKind,
    MatchState,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import (
    EVENT_CLASSIFICATION_REVISION,
    EventCategory,
    EventClassification,
    classify_event,
)
from dhruva.contexts.intelligence.domain.news import (
    NEWS_IDENTITY_REVISION,
    DeduplicationDecision,
    DeduplicationLedger,
    DeduplicationRule,
    NewsFingerprints,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
    normalise_headline,
    significant_tokens,
)
from dhruva.contexts.intelligence.domain.sentiment import (
    SENTIMENT_RULESET_REVISION,
    AbstentionReason,
    SentimentLabel,
    SentimentResult,
    evaluate_sentiment,
)

__all__ = [
    "ENTITY_LINKING_REVISION",
    "EVENT_CLASSIFICATION_REVISION",
    "FORMER_NAME_GRACE",
    "NEWS_IDENTITY_REVISION",
    "SENTIMENT_RULESET_REVISION",
    "AbstentionReason",
    "DeduplicationDecision",
    "DeduplicationLedger",
    "DeduplicationRule",
    "EntityLinkResult",
    "EntityMatch",
    "EventCategory",
    "EventClassification",
    "HistoricalName",
    "LinkableInstrument",
    "MatchKind",
    "MatchState",
    "NewsFingerprints",
    "NewsItem",
    "NewsItemIdentity",
    "NewsSource",
    "NewsSourceTier",
    "PermittedText",
    "SentimentLabel",
    "SentimentResult",
    "canonical_url",
    "classify_event",
    "evaluate_sentiment",
    "link_entities",
    "normalise_headline",
    "significant_tokens",
]
