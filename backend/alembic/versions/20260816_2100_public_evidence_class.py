"""Persist historical dataset evidence class.

Revision ID: 0023_public_evidence_class
Revises: 0022_historical_dataset
Created: 2026-08-16

Reversibility: reversible. Downgrade removes only the explicit classification;
the immutable manifest/file/provenance ledgers and imported historical facts are
retained. Existing rows receive the conservative LICENSED_VENDOR default because
0022 was the licensed/synthetic intake milestone; synthetic manifests continue
to carry their explicit class at the application boundary.
Rollback procedure: export accepted manifest/provenance reports, then run
``alembic downgrade 0022_historical_dataset``; retained deliveries remain usable.
Irreversible operations: none structurally; the explicit classification column
is lost on downgrade and must be recovered from each retained manifest.
Expected runtime: sub-second for the current personal database.
Operational impact: metadata only; candidate-v0 and research facts are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_public_evidence_class"
down_revision: str | None = "0022_historical_dataset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a fail-closed source/evidence classification."""
    op.add_column(
        "historical_dataset",
        sa.Column(
            "evidence_class",
            sa.String(32),
            nullable=False,
            server_default="LICENSED_VENDOR",
        ),
    )
    op.create_check_constraint(
        "ck_historical_dataset_evidence_class",
        "historical_dataset",
        "evidence_class IN ('LICENSED_VENDOR','PUBLIC_EXCHANGE_ARCHIVE',"
        "'PUBLIC_RECONSTRUCTED','OWNER_PROVIDED','SYNTHETIC_TEST')",
    )


def downgrade() -> None:
    """Remove only evidence-class metadata."""
    op.drop_constraint("ck_historical_dataset_evidence_class", "historical_dataset", type_="check")
    op.drop_column("historical_dataset", "evidence_class")
