"""Add PIT historical-universe and corporate-action evidence.

Revision ID: 0021_historical_universe
Revises: 0020_candidate_outcome
Created: 2026-08-16

Reversibility: reversible. Downgrade removes only the new empty/evidence tables.
Rollback procedure: export any licensed dataset manifests and run
``alembic downgrade 0020_candidate_outcome``.
Irreversible operations: none structurally; appended source evidence would need
to be re-imported from its retained licensed dataset after downgrade.
Expected runtime: sub-second while the new tables are empty.
Operational impact: three new tables, two tenant RLS policies, and append-only
mutation guards. Existing observations and candidate-v0 payloads are untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_historical_universe"
down_revision: str | None = "0020_candidate_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINITION = "historical_universe_definition_revision"
MEMBERSHIP = "historical_universe_membership_revision"
ACTION = "corporate_action_revision"
GUARD = "research_observation_reject_mutation"


def _guard(table: str) -> None:
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {GUARD}()"
        )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {GUARD}()"
    )


def upgrade() -> None:
    """Create append-only, bitemporal evidence tables."""
    op.create_table(
        DEFINITION,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("universe_id", sa.String(128), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("source_status", sa.String(32), nullable=False),
        sa.Column("historical_membership_available", sa.Boolean(), nullable=False),
        sa.Column("removals_included", sa.Boolean(), nullable=False),
        sa.Column("delistings_included", sa.Boolean(), nullable=False),
        sa.Column("pit_known_at_available", sa.Boolean(), nullable=False),
        sa.Column("instrument_lifecycle_available", sa.Boolean(), nullable=False),
        sa.Column("licensing_confirmed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "universe_id",
            "source",
            "source_revision",
            name="uq_historical_universe_definition_source_revision",
        ),
        sa.CheckConstraint("btrim(universe_id) <> ''", name="ck_historical_universe_id"),
        sa.CheckConstraint("btrim(label) <> ''", name="ck_historical_universe_label"),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_historical_universe_source"),
        sa.CheckConstraint(
            "pit_known_at_available = false OR historical_membership_available",
            name="ck_historical_universe_pit_requires_history",
        ),
    )
    op.create_index(
        "ix_historical_universe_definition_as_of",
        DEFINITION,
        ["account_id", "universe_id", "known_at"],
    )
    op.create_table(
        MEMBERSHIP,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "definition_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{DEFINITION}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("universe_id", sa.String(128), nullable=False),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("source_member_key", sa.String(200), nullable=False),
        sa.Column("is_delisted", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "universe_id",
            "instrument_id",
            "effective_from",
            "source",
            "source_revision",
            "source_member_key",
            name="uq_historical_membership_source_fact",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_historical_membership_effectivity",
        ),
        sa.CheckConstraint("btrim(source_member_key) <> ''", name="ck_historical_member_key"),
    )
    op.create_index(
        "ix_historical_membership_as_of",
        MEMBERSHIP,
        ["account_id", "universe_id", "effective_from", "known_at"],
    )
    op.create_index(
        "ix_historical_membership_instrument",
        MEMBERSHIP,
        ["instrument_id", "effective_from"],
    )
    op.create_table(
        ACTION,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("verification", sa.String(24), nullable=False),
        sa.Column("ratio_numerator", sa.Numeric(30, 12), nullable=True),
        sa.Column("ratio_denominator", sa.Numeric(30, 12), nullable=True),
        sa.Column("cash_value", sa.Numeric(30, 12), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column(
            "related_instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reference_instrument.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "instrument_id",
            "source",
            "source_revision",
            name="uq_corporate_action_source_revision",
        ),
        sa.CheckConstraint(
            "(ratio_numerator IS NULL) = (ratio_denominator IS NULL)",
            name="ck_corporate_action_ratio_pair",
        ),
        sa.CheckConstraint(
            "ratio_numerator IS NULL OR (ratio_numerator > 0 AND ratio_denominator > 0)",
            name="ck_corporate_action_ratio_positive",
        ),
        sa.CheckConstraint(
            "(cash_value IS NULL) = (currency IS NULL)",
            name="ck_corporate_action_cash_currency",
        ),
    )
    op.create_index(
        "ix_corporate_action_pit", ACTION, ["instrument_id", "effective_date", "known_at"]
    )
    for table in (DEFINITION, MEMBERSHIP):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_account_isolation ON {table} USING (true)")
    for table in (DEFINITION, MEMBERSHIP, ACTION):
        _guard(table)


def downgrade() -> None:
    """Remove the new evidence tables in dependency order."""
    for table in (ACTION, MEMBERSHIP, DEFINITION):
        for operation in ("truncate", "delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{operation} ON {table}")
    for table in (MEMBERSHIP, DEFINITION):
        op.execute(f"DROP POLICY IF EXISTS {table}_account_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_corporate_action_pit", table_name=ACTION)
    op.drop_table(ACTION)
    op.drop_index("ix_historical_membership_instrument", table_name=MEMBERSHIP)
    op.drop_index("ix_historical_membership_as_of", table_name=MEMBERSHIP)
    op.drop_table(MEMBERSHIP)
    op.drop_index("ix_historical_universe_definition_as_of", table_name=DEFINITION)
    op.drop_table(DEFINITION)
