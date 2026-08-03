"""Primitive SQLAlchemy rows owned by the Intelligence context.

Three append-only tables. ``news_item_revision`` is one observed version of one
item; ``news_analysis`` is one pass of the deterministic rulesets over it; and
``news_entity_link`` is the instruments that pass resolved. Nothing is ever
updated, so a correction cannot overwrite the wording a backtest already read.

The title and snippet bounds are enforced twice -- in the domain type and again
as database check constraints -- because the licence position is that DHRUVA
stores a headline and a short extract, and a control that lives only in Python
is a control an ad-hoc script can walk past.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "IntelligenceBase",
    "NewsAnalysisModel",
    "NewsEntityLinkModel",
    "NewsItemRevisionModel",
]

#: Longest permitted stored text, mirroring the domain bounds exactly.
MAX_TITLE = 300
MAX_SNIPPET = 400
_MAX_URL = 2048


class IntelligenceBase(DeclarativeBase):
    """Declarative metadata owned only by the Intelligence context."""


class NewsItemRevisionModel(IntelligenceBase):
    """One observed version of one news item, with both of its timestamps."""

    __tablename__ = "news_item_revision"
    __table_args__ = (
        UniqueConstraint(
            "source_key",
            "provider_item_id",
            "content_revision",
            name="uq_news_item_source_provider_revision",
        ),
        CheckConstraint(
            "source_key ~ '^[a-z][a-z0-9-]{1,63}$'",
            name="ck_news_item_source_key",
        ),
        CheckConstraint(
            "source_tier IN ('OFFICIAL_FILING', 'EXCHANGE_NOTICE', "
            "'ESTABLISHED_PUBLISHER', 'AGGREGATOR', 'UNVERIFIED')",
            name="ck_news_item_source_tier",
        ),
        CheckConstraint("btrim(provider_item_id) <> ''", name="ck_news_item_provider_id"),
        CheckConstraint("canonical_url ~ '^https?://'", name="ck_news_item_canonical_url"),
        CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {MAX_TITLE}",
            name="ck_news_item_title_bounds",
        ),
        # The snippet bound is the licence control, not a storage optimisation:
        # a column that cannot hold an article body will not be talked into
        # holding one by an adapter in a hurry.
        CheckConstraint(
            f"snippet IS NULL OR char_length(snippet) BETWEEN 1 AND {MAX_SNIPPET}",
            name="ck_news_item_snippet_bounds",
        ),
        CheckConstraint(
            "first_seen_at >= published_at",
            name="ck_news_item_observed_after_publication",
        ),
        CheckConstraint(
            "content_revision ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_content_revision",
        ),
        CheckConstraint(
            "fingerprint_identity ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_identity",
        ),
        CheckConstraint("fingerprint_url ~ '^[0-9a-f]{64}$'", name="ck_news_item_fingerprint_url"),
        CheckConstraint(
            "fingerprint_headline ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_headline",
        ),
        CheckConstraint(
            "fingerprint_rewrite IS NULL OR fingerprint_rewrite ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_rewrite",
        ),
        CheckConstraint(
            "duplicate_rule IS NULL OR duplicate_rule IN ('PROVIDER_ITEM_ID', 'CANONICAL_URL', "
            "'REPEATED_FILING', 'SYNDICATED_HEADLINE', 'REWRITTEN_HEADLINE')",
            name="ck_news_item_duplicate_rule",
        ),
        CheckConstraint(
            "(duplicate_rule IS NULL) = (duplicate_of_source_key IS NULL) AND "
            "(duplicate_rule IS NULL) = (duplicate_of_provider_item_id IS NULL) AND "
            "(duplicate_rule IS NULL) = (duplicate_of_url IS NULL)",
            name="ck_news_item_duplicate_pair",
        ),
        CheckConstraint(
            "identity_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_news_item_identity_revision",
        ),
        # The point-in-time read: latest revision of an item observable at a
        # cutoff, which is the only query a backtest is allowed to make.
        Index("ix_news_item_point_in_time", "source_key", "provider_item_id", "first_seen_at"),
        Index("ix_news_item_published_at", "published_at"),
        Index("ix_news_item_fingerprint_headline", "fingerprint_headline"),
        Index("ix_news_item_fingerprint_url", "fingerprint_url"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_tier: Mapped[str] = mapped_column(String(24), nullable=False)
    source_homepage_url: Mapped[str] = mapped_column(String(_MAX_URL), nullable=False)
    provider_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(_MAX_URL), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_url: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_headline: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_rewrite: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duplicate_rule: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The original is named, not hashed. Attribution has to survive being read
    # back, and a digest of an identity cannot be turned into one again.
    duplicate_of_source_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duplicate_of_provider_item_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duplicate_of_url: Mapped[str | None] = mapped_column(String(_MAX_URL), nullable=True)
    duplicate_reason: Mapped[str] = mapped_column(String(240), nullable=False)
    identity_revision: Mapped[str] = mapped_column(String(64), nullable=False)


class NewsAnalysisModel(IntelligenceBase):
    """One deterministic pass of the event, sentiment and linking rulesets."""

    __tablename__ = "news_analysis"
    __table_args__ = (
        UniqueConstraint(
            "news_item_revision_id",
            "event_revision",
            "sentiment_ruleset_revision",
            "linking_revision",
            name="uq_news_analysis_revision_rulesets",
        ),
        CheckConstraint(
            "event_category IN ('EARNINGS_RESULTS', 'GUIDANCE', 'ORDER_WIN', "
            "'MERGER_ACQUISITION', 'CAPITAL_RAISING', 'DIVIDEND', 'SPLIT_BONUS', "
            "'REGULATORY_ACTION', 'LITIGATION', 'FRAUD_GOVERNANCE', 'MANAGEMENT_CHANGE', "
            "'RATING_ACTION', 'PRODUCT_LAUNCH', 'OPERATIONAL_DISRUPTION', 'MACROECONOMIC', "
            "'SECTOR_WIDE', 'GENERAL_COMMENTARY', 'UNKNOWN')",
            name="ck_news_analysis_event_category",
        ),
        CheckConstraint(
            "sentiment_label IN ('POSITIVE', 'NEUTRAL', 'NEGATIVE', 'MIXED', 'UNCERTAIN')",
            name="ck_news_analysis_sentiment_label",
        ),
        CheckConstraint(
            "abstention_reason IS NULL OR abstention_reason IN "
            "('INSUFFICIENT_TEXT', 'NO_LEXICAL_EVIDENCE', 'HEDGED_LANGUAGE')",
            name="ck_news_analysis_abstention_reason",
        ),
        # A decided label with an abstention reason, or an undecided one without,
        # would be a result that contradicts itself.
        CheckConstraint(
            "(sentiment_label IN ('POSITIVE', 'NEGATIVE', 'MIXED')) = (abstention_reason IS NULL)",
            name="ck_news_analysis_abstention_consistency",
        ),
        CheckConstraint(
            "sentiment_score >= -1 AND sentiment_score <= 1",
            name="ck_news_analysis_sentiment_score",
        ),
        CheckConstraint(
            "sentiment_confidence >= 0 AND sentiment_confidence <= 1",
            name="ck_news_analysis_sentiment_confidence",
        ),
        CheckConstraint(
            "mapping_state IN ('MATCHED', 'AMBIGUOUS', 'UNRESOLVED')",
            name="ck_news_analysis_mapping_state",
        ),
        CheckConstraint(
            "sentiment_input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_news_analysis_input_sha256",
        ),
        CheckConstraint(
            "event_revision ~ '^[a-z][a-z0-9_-]{1,63}$' AND "
            "sentiment_ruleset_revision ~ '^[a-z][a-z0-9_-]{1,63}$' AND "
            "linking_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_news_analysis_revisions",
        ),
        Index("ix_news_analysis_item", "news_item_revision_id", "analysed_at"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    news_item_revision_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("news_item_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    analysed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_category: Mapped[str] = mapped_column(String(32), nullable=False)
    event_matched_phrase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_reason: Mapped[str] = mapped_column(String(240), nullable=False)
    event_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    sentiment_label: Mapped[str] = mapped_column(String(16), nullable=False)
    sentiment_score: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    sentiment_confidence: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    abstention_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contrast_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    uncertainty_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Native arrays rather than a delimited string: the matched terms are the
    # explanation a reader checks the label against, and inventing a separator
    # format for them would be one more thing that can be parsed wrongly.
    positive_terms: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String(32)), nullable=False)
    negative_terms: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String(32)), nullable=False)
    negated_terms: Mapped[list[str]] = mapped_column(postgresql.ARRAY(String(32)), nullable=False)
    sentiment_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sentiment_ruleset_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_state: Mapped[str] = mapped_column(String(16), nullable=False)
    mapping_reason: Mapped[str] = mapped_column(String(240), nullable=False)
    linking_revision: Mapped[str] = mapped_column(String(64), nullable=False)


class NewsEntityLinkModel(IntelligenceBase):
    """One instrument an analysis pass tied to one news revision."""

    __tablename__ = "news_entity_link"
    __table_args__ = (
        UniqueConstraint(
            "news_analysis_id",
            "instrument_id",
            "match_state",
            name="uq_news_entity_link_analysis_instrument",
        ),
        CheckConstraint(
            "match_state IN ('MATCHED', 'AMBIGUOUS')",
            name="ck_news_entity_link_match_state",
        ),
        CheckConstraint(
            "match_kind IN ('CANONICAL_SYMBOL', 'COMPANY_NAME', 'ALIAS', 'FORMER_NAME')",
            name="ck_news_entity_link_match_kind",
        ),
        CheckConstraint(
            "relevance > 0 AND relevance <= 1",
            name="ck_news_entity_link_relevance",
        ),
        CheckConstraint("btrim(canonical_symbol) <> ''", name="ck_news_entity_link_symbol"),
        CheckConstraint("btrim(reason) <> ''", name="ck_news_entity_link_reason"),
        Index("ix_news_entity_link_instrument", "instrument_id"),
        Index("ix_news_entity_link_symbol", "canonical_symbol"),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    news_analysis_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("news_analysis.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The stable instrument identity is the key; the symbol is a readable
    # attribute beside it (ADR-009). Deriving an identity from a ticker is the
    # mistake that makes a rename silently repoint years of stored mappings.
    instrument_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    match_state: Mapped[str] = mapped_column(String(16), nullable=False)
    match_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    matched_text: Mapped[str] = mapped_column(String(200), nullable=False)
    relevance: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
