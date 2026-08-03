"""Add the append-only point-in-time news archive.

Revision ID: 0016_news_archive
Revises: 0015_daily_market_bars
Created: 2026-08-03

Reversibility: reversible
Rollback procedure: `alembic downgrade 0015_daily_market_bars` drops the three
    news tables in dependency order. Export any collected news first: the
    archive is prospective, so anything dropped cannot be re-fetched from a free
    source later.
Irreversible operations: none
Expected runtime: sub-second on the initial empty tables.
Operational impact: three new empty global tables and their indexes are created.
    No existing table is rewritten or locked.

Design notes carried into the schema rather than left in a docstring:

  * Nothing here is ever updated. A correction arrives as a new row with a new
    `content_revision` and its own `first_seen_at`, so a query asking what was
    knowable at an earlier instant still reads the earlier wording (ADR-007).
  * `first_seen_at >= published_at` is enforced by the database, because an item
    observed before it was published is a clock defect, not a fact.
  * The title and snippet length bounds are the licence control. A column that
    cannot hold an article body will not be talked into holding one by an
    adapter in a hurry, and enforcing it only in Python leaves it to whoever
    writes the next script.
  * The uniqueness of (source_key, provider_item_id, content_revision) is what
    makes a re-poll idempotent: identical content computes an identical row and
    is discarded, leaving the original observation timestamp in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_news_archive"
down_revision: str | None = "0015_daily_market_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_TITLE = 300
_MAX_SNIPPET = 400
_MAX_URL = 2048


def upgrade() -> None:
    """Create the append-only news revision, analysis and entity-link tables."""
    op.create_table(
        "news_item_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_display_name", sa.String(length=120), nullable=False),
        sa.Column("source_tier", sa.String(length=24), nullable=False),
        sa.Column("source_homepage_url", sa.String(length=_MAX_URL), nullable=False),
        sa.Column("provider_item_id", sa.String(length=200), nullable=False),
        sa.Column("canonical_url", sa.String(length=_MAX_URL), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_revision", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_identity", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_url", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_headline", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_rewrite", sa.String(length=64), nullable=True),
        sa.Column("duplicate_rule", sa.String(length=32), nullable=True),
        sa.Column("duplicate_of_source_key", sa.String(length=64), nullable=True),
        sa.Column("duplicate_of_provider_item_id", sa.String(length=200), nullable=True),
        sa.Column("duplicate_of_url", sa.String(length=_MAX_URL), nullable=True),
        sa.Column("duplicate_reason", sa.String(length=240), nullable=False),
        sa.Column("identity_revision", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "source_key",
            "provider_item_id",
            "content_revision",
            name="uq_news_item_source_provider_revision",
        ),
        sa.CheckConstraint(
            "source_key ~ '^[a-z][a-z0-9-]{1,63}$'",
            name="ck_news_item_source_key",
        ),
        sa.CheckConstraint(
            "source_tier IN ('OFFICIAL_FILING', 'EXCHANGE_NOTICE', "
            "'ESTABLISHED_PUBLISHER', 'AGGREGATOR', 'UNVERIFIED')",
            name="ck_news_item_source_tier",
        ),
        sa.CheckConstraint("btrim(provider_item_id) <> ''", name="ck_news_item_provider_id"),
        sa.CheckConstraint("canonical_url ~ '^https?://'", name="ck_news_item_canonical_url"),
        sa.CheckConstraint(
            f"char_length(title) BETWEEN 1 AND {_MAX_TITLE}",
            name="ck_news_item_title_bounds",
        ),
        sa.CheckConstraint(
            f"snippet IS NULL OR char_length(snippet) BETWEEN 1 AND {_MAX_SNIPPET}",
            name="ck_news_item_snippet_bounds",
        ),
        sa.CheckConstraint(
            "first_seen_at >= published_at",
            name="ck_news_item_observed_after_publication",
        ),
        sa.CheckConstraint(
            "content_revision ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_content_revision",
        ),
        sa.CheckConstraint(
            "fingerprint_identity ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_identity",
        ),
        sa.CheckConstraint(
            "fingerprint_url ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_url",
        ),
        sa.CheckConstraint(
            "fingerprint_headline ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_headline",
        ),
        sa.CheckConstraint(
            "fingerprint_rewrite IS NULL OR fingerprint_rewrite ~ '^[0-9a-f]{64}$'",
            name="ck_news_item_fingerprint_rewrite",
        ),
        sa.CheckConstraint(
            "duplicate_rule IS NULL OR duplicate_rule IN ('PROVIDER_ITEM_ID', 'CANONICAL_URL', "
            "'REPEATED_FILING', 'SYNDICATED_HEADLINE', 'REWRITTEN_HEADLINE')",
            name="ck_news_item_duplicate_rule",
        ),
        sa.CheckConstraint(
            "(duplicate_rule IS NULL) = (duplicate_of_source_key IS NULL) AND "
            "(duplicate_rule IS NULL) = (duplicate_of_provider_item_id IS NULL) AND "
            "(duplicate_rule IS NULL) = (duplicate_of_url IS NULL)",
            name="ck_news_item_duplicate_pair",
        ),
        sa.CheckConstraint(
            "identity_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_news_item_identity_revision",
        ),
    )
    op.create_index(
        "ix_news_item_point_in_time",
        "news_item_revision",
        ["source_key", "provider_item_id", "first_seen_at"],
    )
    op.create_index("ix_news_item_published_at", "news_item_revision", ["published_at"])
    op.create_index(
        "ix_news_item_fingerprint_headline",
        "news_item_revision",
        ["fingerprint_headline"],
    )
    op.create_index("ix_news_item_fingerprint_url", "news_item_revision", ["fingerprint_url"])

    op.create_table(
        "news_analysis",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("news_item_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("analysed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_category", sa.String(length=32), nullable=False),
        sa.Column("event_matched_phrase", sa.String(length=64), nullable=True),
        sa.Column("event_reason", sa.String(length=240), nullable=False),
        sa.Column("event_revision", sa.String(length=64), nullable=False),
        sa.Column("sentiment_label", sa.String(length=16), nullable=False),
        sa.Column("sentiment_score", sa.Numeric(4, 2), nullable=False),
        sa.Column("sentiment_confidence", sa.Numeric(4, 2), nullable=False),
        sa.Column("abstention_reason", sa.String(length=32), nullable=True),
        sa.Column("contrast_present", sa.Boolean(), nullable=False),
        sa.Column("uncertainty_present", sa.Boolean(), nullable=False),
        sa.Column("positive_terms", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("negative_terms", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("negated_terms", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("sentiment_input_sha256", sa.String(length=64), nullable=False),
        sa.Column("sentiment_ruleset_revision", sa.String(length=64), nullable=False),
        sa.Column("mapping_state", sa.String(length=16), nullable=False),
        sa.Column("mapping_reason", sa.String(length=240), nullable=False),
        sa.Column("linking_revision", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_item_revision_id"],
            ["news_item_revision.id"],
            name="fk_news_analysis_revision",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "news_item_revision_id",
            "event_revision",
            "sentiment_ruleset_revision",
            "linking_revision",
            name="uq_news_analysis_revision_rulesets",
        ),
        sa.CheckConstraint(
            "event_category IN ('EARNINGS_RESULTS', 'GUIDANCE', 'ORDER_WIN', "
            "'MERGER_ACQUISITION', 'CAPITAL_RAISING', 'DIVIDEND', 'SPLIT_BONUS', "
            "'REGULATORY_ACTION', 'LITIGATION', 'FRAUD_GOVERNANCE', 'MANAGEMENT_CHANGE', "
            "'RATING_ACTION', 'PRODUCT_LAUNCH', 'OPERATIONAL_DISRUPTION', 'MACROECONOMIC', "
            "'SECTOR_WIDE', 'GENERAL_COMMENTARY', 'UNKNOWN')",
            name="ck_news_analysis_event_category",
        ),
        sa.CheckConstraint(
            "sentiment_label IN ('POSITIVE', 'NEUTRAL', 'NEGATIVE', 'MIXED', 'UNCERTAIN')",
            name="ck_news_analysis_sentiment_label",
        ),
        sa.CheckConstraint(
            "abstention_reason IS NULL OR abstention_reason IN "
            "('INSUFFICIENT_TEXT', 'NO_LEXICAL_EVIDENCE', 'HEDGED_LANGUAGE')",
            name="ck_news_analysis_abstention_reason",
        ),
        sa.CheckConstraint(
            "(sentiment_label IN ('POSITIVE', 'NEGATIVE', 'MIXED')) = (abstention_reason IS NULL)",
            name="ck_news_analysis_abstention_consistency",
        ),
        sa.CheckConstraint(
            "sentiment_score >= -1 AND sentiment_score <= 1",
            name="ck_news_analysis_sentiment_score",
        ),
        sa.CheckConstraint(
            "sentiment_confidence >= 0 AND sentiment_confidence <= 1",
            name="ck_news_analysis_sentiment_confidence",
        ),
        sa.CheckConstraint(
            "mapping_state IN ('MATCHED', 'AMBIGUOUS', 'UNRESOLVED')",
            name="ck_news_analysis_mapping_state",
        ),
        sa.CheckConstraint(
            "sentiment_input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_news_analysis_input_sha256",
        ),
        sa.CheckConstraint(
            "event_revision ~ '^[a-z][a-z0-9_-]{1,63}$' AND "
            "sentiment_ruleset_revision ~ '^[a-z][a-z0-9_-]{1,63}$' AND "
            "linking_revision ~ '^[a-z][a-z0-9_-]{1,63}$'",
            name="ck_news_analysis_revisions",
        ),
    )
    op.create_index(
        "ix_news_analysis_item",
        "news_analysis",
        ["news_item_revision_id", "analysed_at"],
    )

    op.create_table(
        "news_entity_link",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("news_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_symbol", sa.String(length=32), nullable=False),
        sa.Column("match_state", sa.String(length=16), nullable=False),
        sa.Column("match_kind", sa.String(length=24), nullable=False),
        sa.Column("matched_text", sa.String(length=200), nullable=False),
        sa.Column("relevance", sa.Numeric(3, 2), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_analysis_id"],
            ["news_analysis.id"],
            name="fk_news_entity_link_analysis",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "news_analysis_id",
            "instrument_id",
            "match_state",
            name="uq_news_entity_link_analysis_instrument",
        ),
        sa.CheckConstraint(
            "match_state IN ('MATCHED', 'AMBIGUOUS')",
            name="ck_news_entity_link_match_state",
        ),
        sa.CheckConstraint(
            "match_kind IN ('CANONICAL_SYMBOL', 'COMPANY_NAME', 'ALIAS', 'FORMER_NAME')",
            name="ck_news_entity_link_match_kind",
        ),
        sa.CheckConstraint(
            "relevance > 0 AND relevance <= 1",
            name="ck_news_entity_link_relevance",
        ),
        sa.CheckConstraint("btrim(canonical_symbol) <> ''", name="ck_news_entity_link_symbol"),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_news_entity_link_reason"),
    )
    op.create_index("ix_news_entity_link_instrument", "news_entity_link", ["instrument_id"])
    op.create_index("ix_news_entity_link_symbol", "news_entity_link", ["canonical_symbol"])


def downgrade() -> None:
    """Drop the news archive in dependency order."""
    op.drop_index("ix_news_entity_link_symbol", table_name="news_entity_link")
    op.drop_index("ix_news_entity_link_instrument", table_name="news_entity_link")
    op.drop_table("news_entity_link")
    op.drop_index("ix_news_analysis_item", table_name="news_analysis")
    op.drop_table("news_analysis")
    op.drop_index("ix_news_item_fingerprint_url", table_name="news_item_revision")
    op.drop_index("ix_news_item_fingerprint_headline", table_name="news_item_revision")
    op.drop_index("ix_news_item_published_at", table_name="news_item_revision")
    op.drop_index("ix_news_item_point_in_time", table_name="news_item_revision")
    op.drop_table("news_item_revision")
