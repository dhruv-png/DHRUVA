"""TimescaleDB DDL helpers.

Hypertables, compression and retention are **DDL, not model configuration**. They
live in migrations, where they are versioned, reviewable and reversible. Putting
them in a declarative model would make them invisible to Alembic, so the schema a
migration creates and the schema the models describe would silently diverge
(ADR-055).

These helpers emit the statements; a migration calls them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

__all__ = [
    "add_compression_policy",
    "add_retention_policy",
    "create_hypertable",
    "drop_hypertable_policies",
]


def create_hypertable(
    connection: Connection,
    table: str,
    time_column: str,
    *,
    chunk_interval: str = "1 day",
) -> None:
    """Convert a table into a hypertable partitioned on ``time_column``.

    Parameters
    ----------
    chunk_interval
        Sized so one chunk fits comfortably in memory. For tick data at the
        volumes in plan section 2.4 -- roughly 12 GB per trading day raw -- one
        day is the working default; a wider interval makes recent-data queries
        scan chunks that no longer fit in cache.

    Notes
    -----
    ``migrate_data => TRUE`` is deliberately **not** used. It rewrites existing
    rows while holding a lock, which on a large table is an unbounded outage.
    Hypertables are created empty, before the data arrives.
    """
    connection.execute(
        text(
            "SELECT create_hypertable(:table, :time_column, "
            "chunk_time_interval => INTERVAL :chunk_interval, "
            "if_not_exists => TRUE, migrate_data => FALSE)"
        ).bindparams(table=table, time_column=time_column, chunk_interval=chunk_interval)
    )


def add_compression_policy(
    connection: Connection,
    table: str,
    *,
    compress_after: str = "30 days",
    segment_by: str,
    order_by: str,
) -> None:
    """Compress chunks older than ``compress_after``.

    Parameters
    ----------
    segment_by
        The column queries filter on -- ``instrument_id`` for market data.
        Segmenting by it lets compressed chunks be filtered without
        decompressing, which is the difference between compression being free
        and being a query tax.
    order_by
        Usually the time column, descending. Ordering determines how well the
        columnar encoding compresses.

    Notes
    -----
    Compressed chunks are **effectively read-only** in older TimescaleDB
    versions: inserts into them fail or are expensive. The 30-day default exists
    so that late-arriving corrections -- which do happen with exchange data --
    land in uncompressed chunks.
    """
    connection.execute(
        text(
            f"ALTER TABLE {table} SET ("
            "timescaledb.compress = true, "
            f"timescaledb.compress_segmentby = '{segment_by}', "
            f"timescaledb.compress_orderby = '{order_by}')"
        )
    )
    connection.execute(
        text(
            "SELECT add_compression_policy(:table, INTERVAL :after, if_not_exists => TRUE)"
        ).bindparams(table=table, after=compress_after)
    )


def add_retention_policy(connection: Connection, table: str, *, drop_after: str) -> None:
    """Drop chunks older than ``drop_after``.

    **Irreversible.** A retention policy deletes data on a schedule, and a
    migration that adds one must classify itself accordingly (ADR-055). Cold data
    is exported to Parquet *before* the policy is allowed to reach it (plan
    section 2.4); the export is not this function's responsibility, and enabling
    retention without it loses history permanently.
    """
    connection.execute(
        text(
            "SELECT add_retention_policy(:table, INTERVAL :after, if_not_exists => TRUE)"
        ).bindparams(table=table, after=drop_after)
    )


def drop_hypertable_policies(connection: Connection, table: str) -> None:
    """Remove compression and retention policies, for a migration downgrade.

    Removing the policies is reversible. Data a retention policy already dropped
    is not, which is why a migration adding retention cannot honestly call itself
    reversible.
    """
    connection.execute(
        text("SELECT remove_compression_policy(:table, if_exists => TRUE)").bindparams(table=table)
    )
    connection.execute(
        text("SELECT remove_retention_policy(:table, if_exists => TRUE)").bindparams(table=table)
    )
