"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Reversibility: REPLACE -- one of: reversible | conditionally reversible | irreversible
Rollback procedure: REPLACE -- exact steps, or why none exists
Irreversible operations: REPLACE -- name them, or "none"
Expected runtime: REPLACE -- order of magnitude at production volume
Operational impact: REPLACE -- locks taken, whether writes block, maintenance window needed
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the migration."""
    ${upgrades if upgrades else "raise NotImplementedError"}


def downgrade() -> None:
    """Reverse the migration.

    If genuinely irreversible, raise with the reason rather than leaving this
    empty -- an empty downgrade silently claims reversibility it does not have.
    """
    ${downgrades if downgrades else "raise NotImplementedError"}
