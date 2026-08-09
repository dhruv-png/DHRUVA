"""Add the append-only research-attention observation ledger.

Revision ID: 0018_research_observation
Revises: 0017_credential_purpose
Created: 2026-08-09

Reversibility: reversible
Rollback procedure: `alembic downgrade 0017_credential_purpose` removes the
    mutation guards, RLS policies, member table and observation table. Export
    the ledger first: prospective evidence cannot be reconstructed later.
Irreversible operations: none structurally. A downgrade intentionally loses any
    prospective observations already accumulated.
Expected runtime: sub-second on the initially empty tables.
Operational impact: two empty account-owned tables, indexes, permissive v1 RLS
    policies and mutation-refusal triggers are created. No existing table is
    rewritten or locked beyond normal migration catalogue locks.

`research_observation` is the immutable header for one PIT-resolved research
state. `attention_observation_member` records every member of the complete
existing attention ranking, not only the compact brief's top-N. The packet body
hash links the header to the already-defined research-packet contract while the
observation hash covers the complete ranking and source-health provenance.

Identical retries share `(account, type, cutoff, observation_sha256)` and are a
no-op in the repository. A changed input at the same cutoff appends a row whose
composite foreign key can only supersede a row in the same account/type/cutoff
stream. The unique supersession target makes that history a chain rather than a
fork. Neither table permits UPDATE, DELETE or TRUNCATE.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_research_observation"
down_revision: str | None = "0017_credential_purpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVATION_POLICY = "research_observation_account_isolation"
MEMBER_POLICY = "attention_observation_member_account_isolation"
GUARD_FUNCTION = "research_observation_reject_mutation"
SUPERSESSION_FUNCTION = "research_observation_validate_supersession"

_GUARD_SQL = f"""
CREATE FUNCTION {GUARD_FUNCTION}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (ADR-078): % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""

_SUPERSESSION_SQL = """
CREATE FUNCTION research_observation_validate_supersession() RETURNS trigger AS $$
DECLARE
    prior_recorded_at timestamptz;
BEGIN
    IF NEW.supersedes_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT recorded_at INTO prior_recorded_at
      FROM research_observation
     WHERE id = NEW.supersedes_id
       AND account_id = NEW.account_id
       AND observation_type = NEW.observation_type
       AND cutoff = NEW.cutoff;
    IF prior_recorded_at IS NOT NULL AND NEW.recorded_at < prior_recorded_at THEN
        RAISE EXCEPTION 'a research observation cannot predate the fact it supersedes'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    """Create the observation header, ranked members and database guards."""
    op.create_table(
        "research_observation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observation_type", sa.String(length=32), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_status", sa.String(length=16), nullable=False),
        sa.Column("market_source_status", sa.String(length=16), nullable=False),
        sa.Column("news_source_status", sa.String(length=16), nullable=False),
        sa.Column("degraded_reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("attention_revision", sa.String(length=128), nullable=False),
        sa.Column("digest_revision", sa.String(length=128), nullable=False),
        sa.Column("digest_policy_revision", sa.String(length=128), nullable=False),
        sa.Column("entity_linking_revision", sa.String(length=128), nullable=False),
        sa.Column("event_classification_revision", sa.String(length=128), nullable=False),
        sa.Column("market_context_revision", sa.String(length=128), nullable=False),
        sa.Column("news_identity_revision", sa.String(length=128), nullable=False),
        sa.Column("sentiment_revision", sa.String(length=128), nullable=False),
        sa.Column("packet_schema_revision", sa.String(length=128), nullable=False),
        sa.Column("observation_schema_revision", sa.String(length=128), nullable=False),
        sa.Column("universe_sha256", sa.String(length=64), nullable=False),
        sa.Column("observation_sha256", sa.String(length=64), nullable=False),
        sa.Column("packet_body_sha256", sa.String(length=64), nullable=False),
        sa.Column("ranked_count", sa.Integer(), nullable=False),
        sa.Column("attention_count", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_research_observation"),
        sa.UniqueConstraint("id", "account_id", name="uq_research_observation_id_account"),
        sa.UniqueConstraint(
            "id",
            "account_id",
            "observation_type",
            "cutoff",
            name="uq_research_observation_supersession_target",
        ),
        sa.UniqueConstraint(
            "account_id",
            "observation_type",
            "cutoff",
            "observation_sha256",
            name="uq_research_observation_logical_identity",
        ),
        sa.UniqueConstraint("supersedes_id", name="uq_research_observation_supersedes"),
        sa.ForeignKeyConstraint(
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
        sa.CheckConstraint(
            "observation_type IN ('ATTENTION_OBSERVATION')",
            name="ck_research_observation_type",
        ),
        sa.CheckConstraint(
            "run_status IN ('HEALTHY', 'DEGRADED')",
            name="ck_research_observation_run_status",
        ),
        sa.CheckConstraint(
            "market_source_status IN ('HEALTHY', 'DEGRADED', 'REFUSED') AND "
            "news_source_status IN ('HEALTHY', 'DEGRADED', 'REFUSED')",
            name="ck_research_observation_source_status",
        ),
        sa.CheckConstraint(
            "(run_status = 'HEALTHY' AND market_source_status = 'HEALTHY' "
            "AND news_source_status = 'HEALTHY' AND cardinality(degraded_reasons) = 0) OR "
            "(run_status = 'DEGRADED' AND "
            "(market_source_status <> 'HEALTHY' OR news_source_status <> 'HEALTHY') "
            "AND cardinality(degraded_reasons) > 0)",
            name="ck_research_observation_health_consistency",
        ),
        sa.CheckConstraint(
            "recorded_at >= cutoff",
            name="ck_research_observation_chronology",
        ),
        sa.CheckConstraint(
            "ranked_count >= 0 AND attention_count >= 0 AND attention_count <= ranked_count",
            name="ck_research_observation_counts",
        ),
        sa.CheckConstraint(
            "universe_sha256 ~ '^[0-9a-f]{64}$' AND "
            "observation_sha256 ~ '^[0-9a-f]{64}$' AND "
            "packet_body_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_observation_fingerprints",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name="ck_research_no_self",
        ),
    )
    op.create_index(
        "ix_research_observation_account_cutoff",
        "research_observation",
        ["account_id", "cutoff", "recorded_at"],
    )
    op.create_index(
        "ix_research_observation_account_type_cutoff",
        "research_observation",
        ["account_id", "observation_type", "cutoff"],
    )

    op.create_table(
        "attention_observation_member",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("canonical_symbol", sa.String(length=32), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("band", sa.String(length=16), nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("market_context_available", sa.Boolean(), nullable=False),
        sa.Column("market_context_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "archived_news_revisions",
            postgresql.ARRAY(sa.String(length=64)),
            nullable=False,
        ),
        sa.Column("news_items_withheld", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "observation_id",
            "instrument_id",
            name="pk_attention_observation_member",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "account_id"],
            ["research_observation.id", "research_observation.account_id"],
            name="fk_attention_member_observation_account",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "observation_id",
            "rank",
            name="uq_attention_observation_member_rank",
        ),
        sa.CheckConstraint("rank >= 1", name="ck_attention_observation_member_rank"),
        sa.CheckConstraint("score BETWEEN 0 AND 14", name="ck_attention_observation_member_score"),
        sa.CheckConstraint(
            "(score = 0 AND band = 'LOW') OR "
            "(score BETWEEN 1 AND 2 AND band = 'NORMAL') OR "
            "(score BETWEEN 3 AND 6 AND band = 'ELEVATED') OR "
            "(score BETWEEN 7 AND 14 AND band = 'HIGH')",
            name="ck_attention_observation_member_band",
        ),
        sa.CheckConstraint("btrim(canonical_symbol) <> ''", name="ck_attention_member_symbol"),
        sa.CheckConstraint("btrim(company_name) <> ''", name="ck_attention_member_company"),
        sa.CheckConstraint(
            "market_context_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_attention_member_market_fingerprint",
        ),
        sa.CheckConstraint(
            "news_items_withheld >= 0",
            name="ck_attention_member_news_withheld",
        ),
    )
    op.create_index(
        "ix_attention_observation_member_account",
        "attention_observation_member",
        ["account_id", "observation_id", "rank"],
    )

    for table, policy in (
        ("research_observation", OBSERVATION_POLICY),
        ("attention_observation_member", MEMBER_POLICY),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {policy} ON {table} USING (true)")

    op.execute(_GUARD_SQL)
    for table in ("research_observation", "attention_observation_member"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {GUARD_FUNCTION}()"
        )
    op.execute(_SUPERSESSION_SQL)
    op.execute(
        "CREATE TRIGGER research_observation_supersession_chronology "
        "BEFORE INSERT ON research_observation FOR EACH ROW "
        f"EXECUTE FUNCTION {SUPERSESSION_FUNCTION}()"
    )


def downgrade() -> None:
    """Remove the ledger after removing every guard that protects it."""
    op.execute(
        "DROP TRIGGER IF EXISTS research_observation_supersession_chronology "
        "ON research_observation"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {SUPERSESSION_FUNCTION}()")
    for table in ("attention_observation_member", "research_observation"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")

    op.execute(f"DROP POLICY IF EXISTS {MEMBER_POLICY} ON attention_observation_member")
    op.execute("ALTER TABLE attention_observation_member DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_attention_observation_member_account",
        table_name="attention_observation_member",
    )
    op.drop_table("attention_observation_member")

    op.execute(f"DROP POLICY IF EXISTS {OBSERVATION_POLICY} ON research_observation")
    op.execute("ALTER TABLE research_observation DISABLE ROW LEVEL SECURITY")
    op.drop_index(
        "ix_research_observation_account_type_cutoff",
        table_name="research_observation",
    )
    op.drop_index("ix_research_observation_account_cutoff", table_name="research_observation")
    op.drop_table("research_observation")
