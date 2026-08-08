"""Distinguish credential purposes so one broker can hold two secret lifecycles.

Revision ID: 0017_credential_purpose
Revises: 0016_news_archive
Created: 2026-08-07

Reversibility: reversible
Rollback procedure: `alembic downgrade 0016_news_archive` drops `purpose` and
    restores the `(account_id, broker)` uniqueness. **Downgrade fails loudly if
    any account holds more than one credential for one broker**, because
    collapsing them would have to discard a row, and the row it would discard
    holds ciphertext nobody can regenerate. Delete the unwanted credential
    explicitly first if that is really the intent.
Irreversible operations: none. No ciphertext is read, rewritten or re-sealed by
    this migration in either direction.
Expected runtime: sub-second. The table is empty in every environment that
    exists today (see the compatibility note below), and would be a handful of
    rows even in a mature one.
Operational impact: one new NOT NULL column with a check constraint, one unique
    constraint replaced by a wider one. Existing ciphertext is untouched.

Compatibility with existing rows
--------------------------------
There are none, and this is a statement of fact rather than an assumption. No
composition root writes a credential: nothing under `src/` constructs a
`Credential` outside the persistence factory that reconstructs one on read, no
enrolment use case exists, and no CLI reaches the credential store. Every
credential row that has ever existed was created inside a test and dropped with
its transaction.

So the backfill is unconditional and cannot misclassify anything: any row that
somehow exists predates the distinction entirely, and the only material the
product was ever going to store under the old shape is enrolment material -- the
session concept did not exist until this revision. The `server_default` is
therefore applied and then dropped, leaving the column NOT NULL with no default,
so a future insert must state its purpose rather than inherit one.

Had rows existed with two possible meanings, this migration would have been the
wrong place to guess: re-sealing ciphertext under a new binding requires the
master key, which a migration does not have and should not.

Why the associated data changed with the schema
-----------------------------------------------
ADR-077. `purpose` is bound into the AES-GCM associated data, and the binding's
version tag moved from v1 to v2 in the same change. Any ciphertext sealed under
v1 now fails authentication rather than opening under a binding that no longer
distinguishes an owner's long-lived secret from a day's session token. With no
rows in existence that costs nothing today, and it is the behaviour that would
have been wanted had there been rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_credential_purpose"
down_revision: str | None = "0016_news_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the purpose column and widen the uniqueness rule to include it."""
    op.add_column(
        "credential",
        sa.Column(
            "purpose",
            sa.String(length=16),
            nullable=False,
            # Applied so the NOT NULL can be added in one statement, then
            # dropped below: a lasting default would let a future insert acquire
            # a purpose it never stated, which is exactly the silent
            # classification this column exists to prevent.
            server_default="ENROLMENT",
        ),
    )
    op.alter_column("credential", "purpose", server_default=None)
    # Written out rather than interpolated from the domain enum. A migration is
    # a historical record: it must keep meaning what it meant when it ran, even
    # after the enum grows a third member that this revision never allowed.
    op.create_check_constraint(
        "ck_credential_purpose",
        "credential",
        "purpose IN ('ENROLMENT', 'SESSION')",
    )
    op.drop_constraint("uq_credential_account_broker", "credential", type_="unique")
    op.create_unique_constraint(
        "uq_credential_account_broker_purpose",
        "credential",
        ["account_id", "broker", "purpose"],
    )


def downgrade() -> None:
    """Restore the narrower uniqueness, refusing to discard a credential.

    The guard is the point. Collapsing `(account_id, broker, purpose)` back to
    `(account_id, broker)` is only possible when no account holds two purposes
    for one broker, and the alternative -- picking one row to delete -- destroys
    ciphertext that cannot be regenerated. Failing here is recoverable; a
    downgrade that silently dropped a credential is not.
    """
    conflicting = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT account_id, broker FROM credential"
                "  GROUP BY account_id, broker HAVING count(*) > 1"
                ") AS duplicated"
            )
        )
        .scalar_one()
    )
    if conflicting:
        message = (
            f"{conflicting} account/broker pair(s) hold more than one credential "
            "purpose. Downgrading would have to discard sealed material that "
            "cannot be regenerated. Remove the unwanted credentials explicitly, "
            "then retry."
        )
        raise RuntimeError(message)

    op.drop_constraint("uq_credential_account_broker_purpose", "credential", type_="unique")
    op.create_unique_constraint(
        "uq_credential_account_broker",
        "credential",
        ["account_id", "broker"],
    )
    op.drop_constraint("ck_credential_purpose", "credential", type_="check")
    op.drop_column("credential", "purpose")
