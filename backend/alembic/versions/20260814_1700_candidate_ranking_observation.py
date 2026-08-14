"""Add append-only experimental candidate-ranking observations.

Revision ID: 0019_candidate_ranking
Revises: 0018_research_observation
Created: 2026-08-14

Reversibility: reversible
Rollback procedure: `alembic downgrade 0018_research_observation` removes the
    candidate mutation guards, policy, index, and table. Export official
    candidate freezes before rollback because they cannot be reconstructed as
    prospectively recorded facts.
Irreversible operations: none structurally. Downgrade intentionally discards
    any candidate freezes that have not been exported.
Expected runtime: sub-second while the new table is empty.
Operational impact: creates one empty account-owned table, one index, a
    permissive-v1 RLS policy, and update/delete/truncate refusal triggers. No
    existing attention row or table is rewritten.

The dedicated payload is necessary because the v1 attention header has
mandatory attention/news provenance and an attention-specific member schema;
coercing candidate facts into those columns would destroy their meaning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_candidate_ranking"
down_revision: str | None = "0018_research_observation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "candidate_ranking_observation"
POLICY = "candidate_ranking_observation_account_isolation"
GUARD = "research_observation_reject_mutation"


def upgrade() -> None:
    """Create one complete JSONB-backed candidate freeze header."""
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ranker_revision", sa.String(length=128), nullable=False),
        sa.Column("feature_revision", sa.String(length=128), nullable=False),
        sa.Column("schema_revision", sa.String(length=128), nullable=False),
        sa.Column("benchmark_symbol", sa.String(length=32), nullable=False),
        sa.Column("benchmark_basis", sa.String(length=32), nullable=False),
        sa.Column("universe_label", sa.String(length=64), nullable=False),
        sa.Column("universe_sha256", sa.String(length=64), nullable=False),
        sa.Column("observation_sha256", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_ranking_observation"),
        sa.UniqueConstraint(
            "account_id",
            "cutoff",
            "ranker_revision",
            "observation_sha256",
            name="uq_candidate_observation_logical_identity",
        ),
        sa.CheckConstraint("recorded_at >= cutoff", name="ck_candidate_observation_chronology"),
        sa.CheckConstraint(
            "universe_sha256 ~ '^[0-9a-f]{64}$' AND observation_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_observation_fingerprints",
        ),
        sa.CheckConstraint(
            "ranker_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "feature_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "schema_revision ~ '^[a-z][a-z0-9._-]{1,127}$'",
            name="ck_candidate_observation_revisions",
        ),
        sa.CheckConstraint(
            "eligible_count >= 0 AND member_count >= eligible_count",
            name="ck_candidate_observation_counts",
        ),
    )
    op.create_index(
        "ix_candidate_observation_account_cutoff",
        TABLE,
        ["account_id", "cutoff", "recorded_at"],
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY} ON {TABLE} USING (true)")
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER {TABLE}_no_{operation.lower()} BEFORE {operation} ON {TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {GUARD}()"
        )
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {GUARD}()"
    )


def downgrade() -> None:
    """Remove the candidate freeze table without touching attention history."""
    for operation in ("truncate", "delete", "update"):
        op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_{operation} ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_candidate_observation_account_cutoff", table_name=TABLE)
    op.drop_table(TABLE)
