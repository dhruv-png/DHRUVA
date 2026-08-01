"""Alembic environment, async-aware.

Imports every model module so ``--autogenerate`` sees the complete metadata. A
model that is never imported is invisible to autogenerate, and the resulting
migration silently omits its table -- which is why the imports below are explicit
rather than discovered.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from dhruva.contexts.platform.infrastructure.database.engine import database_url
from dhruva.contexts.platform.infrastructure.persistence import (  # noqa: F401
    ledger,
    models,
    outbox,
)
from dhruva.contexts.platform.infrastructure.persistence.models import Base
from dhruva.shared.config.settings import load_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# The URL comes from validated settings rather than alembic.ini, so a credential
# never lands in a committed file (ADR-031, ADR-033).
config.set_main_option("sqlalchemy.url", database_url(load_settings().db))


def _configure(connection: Connection) -> None:
    """Configure the migration context.

    ``compare_type`` and ``compare_server_default`` are on so autogenerate
    detects a column whose type or default drifted, not only one that appeared or
    vanished. Without them the "autogenerate produces an empty diff" check would
    pass while the schema and the models disagreed about a column's type.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL without a connection, for review before applying."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply migrations against a live database.

    ``NullPool`` because a migration run is short-lived and pooling would hold
    connections open after it finishes -- which matters when CI runs
    upgrade/downgrade cycles back to back against a container.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
