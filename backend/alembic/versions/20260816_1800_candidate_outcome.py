"""Add append-only matured candidate outcomes.

Revision ID: 0020_candidate_outcome
Revises: 0019_candidate_ranking
Created: 2026-08-16

Reversibility: reversible
Rollback procedure: export model evidence, then run
    `alembic downgrade 0019_candidate_ranking`; the outcome table, policy,
    guards, index, and parent composite key are removed.
Irreversible operations: none structurally. Downgrade discards prospectively
    materialized outcomes, which cannot be recreated with their original
    knowledge timestamp after the fact.
Expected runtime: sub-second while the outcome table is empty.
Operational impact: a brief metadata lock adds one parent uniqueness constraint;
    the new child table starts empty. Existing candidate payloads are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_candidate_outcome"
down_revision: str | None = "0019_candidate_ranking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "candidate_outcome"
PARENT = "candidate_ranking_observation"
POLICY = "candidate_outcome_account_isolation"
GUARD = "research_observation_reject_mutation"


def upgrade() -> None:
    """Create account-bound outcome facts and database mutation guards."""
    op.create_unique_constraint(
        "uq_candidate_observation_id_account",
        PARENT,
        ["id", "account_id"],
    )
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_observation_sha256", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_symbol", sa.String(length=32), nullable=False),
        sa.Column("signal_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observable_through", sa.Date(), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_price", sa.Numeric(50, 28), nullable=False),
        sa.Column("exit_date", sa.Date(), nullable=False),
        sa.Column("exit_price", sa.Numeric(50, 28), nullable=False),
        sa.Column("absolute_return", sa.Numeric(50, 28), nullable=False),
        sa.Column("benchmark_return", sa.Numeric(50, 28), nullable=False),
        sa.Column("excess_return", sa.Numeric(50, 28), nullable=False),
        sa.Column("net_return", sa.Numeric(50, 28), nullable=False),
        sa.Column("net_excess_return", sa.Numeric(50, 28), nullable=False),
        sa.Column("maximum_adverse_excursion", sa.Numeric(50, 28), nullable=False),
        sa.Column("maximum_favorable_excursion", sa.Numeric(50, 28), nullable=False),
        sa.Column("holding_period_drawdown", sa.Numeric(50, 28), nullable=False),
        sa.Column("realized_volatility", sa.Numeric(50, 28), nullable=False),
        sa.Column("ranker_revision", sa.String(length=128), nullable=False),
        sa.Column("feature_revision", sa.String(length=128), nullable=False),
        sa.Column("evaluation_revision", sa.String(length=128), nullable=False),
        sa.Column("outcome_revision", sa.String(length=128), nullable=False),
        sa.Column("schema_revision", sa.String(length=128), nullable=False),
        sa.Column("benchmark_symbol", sa.String(length=32), nullable=False),
        sa.Column("benchmark_basis", sa.String(length=32), nullable=False),
        sa.Column("execution_timing", sa.String(length=128), nullable=False),
        sa.Column("cost_bps", sa.Numeric(12, 4), nullable=False),
        sa.Column("adjustment_status", sa.String(length=16), nullable=False),
        sa.Column("limitations", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("stock_bar_revisions", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column(
            "benchmark_bar_revisions", postgresql.ARRAY(sa.String(length=64)), nullable=False
        ),
        sa.Column("outcome_sha256", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_outcome"),
        sa.ForeignKeyConstraint(
            ["candidate_observation_id", "account_id"],
            [f"{PARENT}.id", f"{PARENT}.account_id"],
            name="fk_candidate_outcome_observation_account",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "account_id",
            "candidate_observation_id",
            "instrument_id",
            "horizon_sessions",
            "outcome_revision",
            "observable_through",
            "outcome_sha256",
            name="uq_candidate_outcome_logical_identity",
        ),
        sa.CheckConstraint("horizon_sessions IN (20, 60)", name="ck_candidate_outcome_horizon"),
        sa.CheckConstraint("entry_date > signal_cutoff::date", name="ck_candidate_outcome_entry"),
        sa.CheckConstraint("exit_date >= entry_date", name="ck_candidate_outcome_exit"),
        sa.CheckConstraint(
            "observable_through >= exit_date AND materialized_at::date >= observable_through",
            name="ck_candidate_outcome_maturity",
        ),
        sa.CheckConstraint("cost_bps >= 0", name="ck_candidate_outcome_cost"),
        sa.CheckConstraint(
            "cardinality(stock_bar_revisions) > 0 AND cardinality(benchmark_bar_revisions) > 0",
            name="ck_candidate_outcome_provenance",
        ),
        sa.CheckConstraint(
            "candidate_observation_sha256 ~ '^[0-9a-f]{64}$' AND outcome_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_outcome_fingerprints",
        ),
        sa.CheckConstraint(
            "ranker_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "feature_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "evaluation_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "outcome_revision ~ '^[a-z][a-z0-9._-]{1,127}$' AND "
            "schema_revision ~ '^[a-z][a-z0-9._-]{1,127}$'",
            name="ck_candidate_outcome_revisions",
        ),
    )
    op.create_index(
        "ix_candidate_outcome_account_cutoff_horizon",
        TABLE,
        ["account_id", "signal_cutoff", "horizon_sessions"],
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
    """Remove outcome evidence without changing frozen candidate rankings."""
    for operation in ("truncate", "delete", "update"):
        op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_{operation} ON {TABLE}")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_candidate_outcome_account_cutoff_horizon", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_constraint("uq_candidate_observation_id_account", PARENT, type_="unique")
