"""Add licensed historical-dataset intake and row provenance.

Revision ID: 0022_historical_dataset
Revises: 0021_historical_universe
Created: 2026-08-16

Reversibility: reversible. Downgrade removes the intake ledger and the four
new universe evidence fields; retained delivery files remain outside the DB.
Rollback procedure: export any accepted manifest/provenance report, then run
``alembic downgrade 0021_historical_universe`` and retain the source delivery.
Irreversible operations: none structurally; removing imported rows loses the
database index but the retained immutable delivery remains re-importable.
Expected runtime: sub-second before any historical dataset is imported.
Operational impact: four append-only, tenant-scoped tables and four nullable-
safe universe columns with conservative defaults. Existing candidate-v0 facts
and research observations are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_historical_dataset"
down_revision: str | None = "0021_historical_universe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINITION = "historical_universe_definition_revision"
DATASET = "historical_dataset"
FILE = "historical_dataset_file"
PROVENANCE = "historical_dataset_fact_provenance"
RUN = "historical_import_run"
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
    """Create immutable dataset, file, row-provenance and run ledgers."""
    op.add_column(
        DEFINITION,
        sa.Column(
            "corporate_action_coverage_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        DEFINITION,
        sa.Column("return_basis", sa.String(24), nullable=False, server_default="UNKNOWN"),
    )
    op.add_column(
        DEFINITION,
        sa.Column("benchmark_basis", sa.String(24), nullable=False, server_default="PRICE_INDEX"),
    )
    op.add_column(
        DEFINITION,
        sa.Column(
            "benchmark_history_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_check_constraint(
        "ck_historical_universe_return_basis",
        DEFINITION,
        "return_basis IN ('RAW_PRICE','PRICE_ADJUSTED','TOTAL_RETURN','UNKNOWN')",
    )
    op.create_check_constraint(
        "ck_historical_universe_benchmark_basis",
        DEFINITION,
        "benchmark_basis IN ('PRICE_INDEX','TOTAL_RETURN_INDEX')",
    )

    op.create_table(
        DATASET,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.String(32), nullable=False),
        sa.Column("provider_name", sa.String(200), nullable=False),
        sa.Column("provider_product", sa.String(200), nullable=False),
        sa.Column("license_reference", sa.String(256), nullable=False),
        sa.Column("licensing_status", sa.String(40), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("manifest_schema", sa.String(64), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(64), nullable=False),
        sa.Column("integrity_status", sa.String(32), nullable=False),
        sa.Column("import_policy", sa.String(24), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("timezone", sa.String(40), nullable=False),
        sa.Column("price_basis", sa.String(24), nullable=False),
        sa.Column("adjustment_basis", sa.String(32), nullable=False),
        sa.Column("return_basis", sa.String(24), nullable=False),
        sa.Column("benchmark_basis", sa.String(24), nullable=False),
        sa.Column("known_at_semantics", sa.String(32), nullable=False),
        sa.Column("capability_claims", postgresql.JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "dataset_id",
            "source_revision",
            name="uq_historical_dataset_source_revision",
        ),
        sa.CheckConstraint("btrim(dataset_id) <> ''", name="ck_historical_dataset_id"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_historical_dataset_sha"),
        sa.CheckConstraint("coverage_end >= coverage_start", name="ck_historical_dataset_coverage"),
    )
    op.create_index(
        "ix_historical_dataset_account", DATASET, ["account_id", "dataset_id", "imported_at"]
    )
    op.create_table(
        FILE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{DATASET}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("schema_revision", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.UniqueConstraint("dataset_revision_id", "role", name="uq_historical_dataset_file_role"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_historical_dataset_file_sha"),
        sa.CheckConstraint("size_bytes > 0", name="ck_historical_dataset_file_size"),
        sa.CheckConstraint("row_count >= 0", name="ck_historical_dataset_file_rows"),
    )
    op.create_table(
        PROVENANCE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{DATASET}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dataset_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{FILE}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_role", sa.String(40), nullable=False),
        sa.Column("fact_key", sa.String(512), nullable=False),
        sa.Column("source_row_id", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.String(32), nullable=False),
        sa.Column("provider_revision", sa.String(128), nullable=False),
        sa.Column("source_known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("schema_revision", sa.String(64), nullable=False),
        sa.Column("mapping_revision", sa.String(64), nullable=False),
        sa.Column("fact_revision", sa.String(128), nullable=False),
        sa.UniqueConstraint(
            "dataset_revision_id",
            "fact_role",
            "source_row_id",
            name="uq_historical_fact_source_row",
        ),
    )
    op.create_index("ix_historical_fact_key", PROVENANCE, ["account_id", "fact_role", "fact_key"])
    op.create_table(
        RUN,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{DATASET}.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("preflight_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_historical_import_run_dataset", RUN, ["account_id", "dataset_revision_id", "started_at"]
    )
    for table in (DATASET, FILE, PROVENANCE, RUN):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_account_isolation ON {table} USING (true)")
        _guard(table)


def downgrade() -> None:
    """Remove intake persistence and conservative universe metadata."""
    for table in (RUN, PROVENANCE, FILE, DATASET):
        for operation in ("truncate", "delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{operation} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_account_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_historical_import_run_dataset", table_name=RUN)
    op.drop_table(RUN)
    op.drop_index("ix_historical_fact_key", table_name=PROVENANCE)
    op.drop_table(PROVENANCE)
    op.drop_table(FILE)
    op.drop_index("ix_historical_dataset_account", table_name=DATASET)
    op.drop_table(DATASET)
    op.drop_constraint("ck_historical_universe_benchmark_basis", DEFINITION, type_="check")
    op.drop_constraint("ck_historical_universe_return_basis", DEFINITION, type_="check")
    for column in (
        "benchmark_history_available",
        "benchmark_basis",
        "return_basis",
        "corporate_action_coverage_available",
    ):
        op.drop_column(DEFINITION, column)
