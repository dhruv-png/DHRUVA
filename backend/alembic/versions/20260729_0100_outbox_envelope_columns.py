"""Complete the ADR-002 envelope on the outbox table.

Revision ID: 0003_outbox_envelope
Revises: 0002_example_tick
Created: 2026-07-29

Reversibility: reversible
Rollback procedure: `alembic downgrade 0002_example_tick` drops the three
    columns. Any correlation or causation data recorded in them is destroyed,
    which is stated rather than assumed: the rows themselves survive, but their
    causal chain does not. Downgrade only where that loss is acceptable.
Irreversible operations: none structurally; see the note above on data.
Expected runtime: sub-second. `ADD COLUMN` with no default and no NOT NULL does
    not rewrite the table on PostgreSQL 11+.
Operational impact: ACCESS EXCLUSIVE lock held only for the catalogue update, so
    the pause is measured in milliseconds. Safe during market hours.

Why this migration exists
-------------------------
ADR-002 fixed the event envelope in Phase 0 as nine fields. The table S04 created
carries six of them: `event_version`, `correlation_id` and `causation_id` were
never added. The outbox as built could not satisfy ADR-002, which means the
platform could not have produced a compliant envelope even once a relay existed.

Why the columns are nullable here
---------------------------------
Expand/contract (ADR-055). Existing rows have no value for any of the three and
inventing one would be fabricating provenance. New writers populate them; a later
contract migration adds NOT NULL once no writer omits them, and that step is
recorded as TD-S05-7 rather than left to memory.

`event_version` takes a server default of 1 because every event already written
is, by definition, version 1 of its schema -- that is a statement of fact rather
than a guess. `correlation_id` and `causation_id` take no default: a fabricated
correlation is worse than an absent one, because it asserts a relationship that
was never observed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_outbox_envelope"
down_revision: str | None = "0002_example_tick"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three missing ADR-002 envelope columns."""
    op.add_column(
        "outbox",
        sa.Column("event_version", sa.Integer(), nullable=True, server_default=sa.text("1")),
    )
    op.add_column(
        "outbox",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "outbox",
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Causal-chain lookup: "what did this event cause?" is the question an
    # EvidenceBundle asks (ADR-018), and without an index it is a sequential scan
    # of every event the platform has ever emitted.
    op.create_index("ix_outbox_causation", "outbox", ["causation_id"])
    op.create_index("ix_outbox_correlation", "outbox", ["correlation_id"])


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    op.drop_index("ix_outbox_correlation", table_name="outbox")
    op.drop_index("ix_outbox_causation", table_name="outbox")
    op.drop_column("outbox", "causation_id")
    op.drop_column("outbox", "correlation_id")
    op.drop_column("outbox", "event_version")
