"""The append-only timeseries worked example (ADR-054).

Revision ID: 0002_example_tick
Revises: 0001_initial
Created: 2026-07-29

Reversibility: reversible
Rollback procedure: `alembic downgrade 0001_initial` drops the table and its
    index. The table is append-only and holds observations rather than records
    of decisions, so destroying it destroys no state any aggregate depends on --
    which is why this downgrade is complete rather than merely structural.
Irreversible operations: none
Expected runtime: sub-second. The table is created empty and nothing existing is
    altered.
Operational impact: no locks on existing objects. Safe during market hours.

Why a second migration rather than an edit to 0001
--------------------------------------------------
0001 has been applied. Editing an applied migration makes the schema a function
of when you happened to run it (ADR-055), so this arrives as its own expand-only
step.

Why no hypertable here
----------------------
`create_hypertable` requires the TimescaleDB extension, and making the DDL
conditional on its presence would make the schema differ between environments --
which the drift check would then report as drift on whichever environment lost
the coin toss. This table exists to exercise the ORM-bypass *write path*, which
is plain PostgreSQL. Hypertable DDL arrives with the real tick table in S10,
where the retention and compression policies that justify it are also decided.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_example_tick"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the example_tick table and its lookup index."""
    op.create_table(
        "example_tick",
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_scaled_units", sa.BigInteger(), nullable=False),
        sa.Column("quantity_units", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("quantity_units > 0", name="ck_example_tick_quantity"),
    )
    op.create_index(
        "ix_example_tick_instrument_observed",
        "example_tick",
        ["instrument_id", "observed_at"],
    )


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    op.drop_index("ix_example_tick_instrument_observed", table_name="example_tick")
    op.drop_table("example_tick")
