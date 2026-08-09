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
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AttentionObservationMemberModel",
    "IntelligenceBase",
    "NewsAnalysisModel",
    "NewsEntityLinkModel",
    "NewsItemRevisionModel",
    "ResearchObservationModel",
]

#: Longest permitted stored text, mirroring the domain bounds exactly.
MAX_TITLE = 300
MAX_SNIPPET = 400
_MAX_URL = 2048


class IntelligenceBase(DeclarativeBase):
    """Declarative metadata owned only by the Intelligence context."""


class ResearchObservationModel(IntelligenceBase):
    """One immutable account-owned research state at one PIT cutoff."""

    __tablename__ = "research_observation"
    __table_args__ = (
        UniqueConstraint("id", "account_id", name="uq_research_observation_id_account"),
        UniqueConstraint(
            "id",
            "account_id",
            "observation_type",
            "cutoff",
            name="uq_research_observation_supersession_target",
        ),
        UniqueConstraint(
            "account_id",
            "observation_type",
            "cutoff",
            "observation_sha256",
            name="uq_research_observation_logical_identity",
        ),
        UniqueConstraint("supersedes_id", name="uq_research_observation_supersedes"),
        ForeignKeyConstraint(
            ["supersedes_id", "account_id", "observation_type", "cutoff"],
            [
                "research_observation.id",
                "research_observation.account_id",
                "research_observation.observation_type",
                "research_observation.cutoff",
            ],
            name="fk_research_observation_supersedes_same_stream",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "observation_type IN ('ATTENTION_OBSERVATION')",
            name="ck_research_observation_type",
        ),
        CheckConstraint(
            "run_status IN ('HEALTHY', 'DEGRADED')",
            name="ck_research_observation_run_status",
        ),
        CheckConstraint(
            "market_source_status IN ('HEALTHY', 'DEGRADED', 'REFUSED') AND "
            "news_source_status IN ('HEALTHY', 'DEGRADED', 'REFUSED')",
            name="ck_research_observation_source_status",
        ),
        CheckConstraint(
            "(run_status = 'HEALTHY' AND market_source_status = 'HEALTHY' "
            "AND news_source_status = 'HEALTHY' AND cardinality(degraded_reasons) = 0) OR "
            "(run_status = 'DEGRADED' AND "
            "(market_source_status <> 'HEALTHY' OR news_source_status <> 'HEALTHY') "
            "AND cardinality(degraded_reasons) > 0)",
            name="ck_research_observation_health_consistency",
        ),
        CheckConstraint("recorded_at >= cutoff", name="ck_research_observation_chronology"),
        CheckConstraint(
            "ranked_count >= 0 AND attention_count >= 0 AND attention_count <= ranked_count",
            name="ck_research_observation_counts",
        ),
        CheckConstraint(
            "universe_sha256 ~ '^[0-9a-f]{64}$' AND "
            "observation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "packet_body_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_observation_fingerprints",
        ),
        CheckConstraint(
            "attention_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "digest_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "digest_policy_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "entity_linking_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "event_classification_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "market_context_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "news_identity_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "sentiment_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "packet_schema_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "observation_schema_revision ~ '^[a-z][a-z0-9._-]{1,127}$'",
            name="ck_research_observation_revisions",
        ),
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="ck_research_no_self"),
        Index(
            "ix_research_observation_account_cutoff",
            "account_id",
            "cutoff",
            "recorded_at",
        ),
        Index(
            "ix_research_observation_account_type_cutoff",
            "account_id",
            "observation_type",
            "cutoff",
        ),
    )

    id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    observation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_status: Mapped[str] = mapped_column(String(16), nullable=False)
    market_source_status: Mapped[str] = mapped_column(String(16), nullable=False)
    news_source_status: Mapped[str] = mapped_column(String(16), nullable=False)
    degraded_reasons: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text), nullable=False)
    attention_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    digest_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    digest_policy_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_linking_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    event_classification_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    market_context_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    news_identity_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    sentiment_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    packet_schema_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_schema_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    universe_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    packet_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ranked_count: Mapped[int] = mapped_column(nullable=False)
    attention_count: Mapped[int] = mapped_column(nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)


class AttentionObservationMemberModel(IntelligenceBase):
    """One immutable member of one attention observation's complete ranking."""

    __tablename__ = "attention_observation_member"
    __table_args__ = (
        ForeignKeyConstraint(
            ["observation_id", "account_id"],
            ["research_observation.id", "research_observation.account_id"],
            name="fk_attention_member_observation_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "observation_id",
            "rank",
            name="uq_attention_observation_member_rank",
        ),
        CheckConstraint("rank >= 1", name="ck_attention_observation_member_rank"),
        CheckConstraint(
            "score BETWEEN 0 AND 14",
            name="ck_attention_observation_member_score",
        ),
        CheckConstraint(
            "(score = 0 AND band = 'LOW') OR "
            "(score BETWEEN 1 AND 2 AND band = 'NORMAL') OR "
            "(score BETWEEN 3 AND 6 AND band = 'ELEVATED') OR "
            "(score BETWEEN 7 AND 14 AND band = 'HIGH')",
            name="ck_attention_observation_member_band",
        ),
        CheckConstraint("btrim(canonical_symbol) <> ''", name="ck_attention_member_symbol"),
        CheckConstraint("btrim(company_name) <> ''", name="ck_attention_member_company"),
        CheckConstraint(
            "market_context_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_attention_member_market_fingerprint",
        ),
        CheckConstraint("news_items_withheld >= 0", name="ck_attention_member_news_withheld"),
        Index(
            "ix_attention_observation_member_account",
            "account_id",
            "observation_id",
            "rank",
        ),
    )

    observation_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)
    canonical_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    score: Mapped[int] = mapped_column(nullable=False)
    band: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text), nullable=False)
    market_context_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    market_context_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_news_revisions: Mapped[list[str]] = mapped_column(
        postgresql.ARRAY(String(64)), nullable=False
    )
    news_items_withheld: Mapped[int] = mapped_column(nullable=False)


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
