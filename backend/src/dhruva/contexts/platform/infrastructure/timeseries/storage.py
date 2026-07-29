"""PostgreSQL bulk append for append-only column data (ADR-054).

Why this exists
---------------
Mapping every tick through a domain object would make S10's ingest path 10-50x
more expensive for nothing: a tick has no identity, no lifecycle, and no
invariant beyond its column constraints. ADR-054 permits exactly one bypass of
the mapping layer, terminating at
:class:`dhruva.shared.persistence.TimeSeriesStorage`, and this is the
implementation behind that interface.

Why ``COPY`` rather than ``INSERT``
-----------------------------------
``COPY`` sends the whole batch in PostgreSQL's binary format in a single
statement, skipping per-row parse, plan and network round trips. For the volumes
this path exists to serve -- a full NSE session is millions of ticks -- the
difference is not a micro-optimisation, it is the difference between keeping up
with the feed and not.

The trade-off is that ``COPY`` bypasses the SQLAlchemy Core expression layer, so
identifiers reach PostgreSQL as text. Everything addressable is therefore
checked against a declared allowlist before any string reaches the driver; see
:func:`_resolve_columns`.

What this class deliberately cannot do
--------------------------------------
There is no update, no delete, and no lookup by identity. Their absence is the
interface enforcing what ADR-054 says, and it is why this is not simply "a
faster repository". Rule R8 fails the build if anything in this package imports
a domain layer, so a business entity cannot be named on this path even by
accident.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = ["PostgresTimeSeriesStorage", "UnknownTimeSeriesTableError"]

#: A conservative unquoted-identifier pattern. Anything outside it is rejected
#: rather than escaped: this path has no legitimate need for exotic names, and
#: "reject" is a shorter argument to audit than "escape correctly".
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


class UnknownTimeSeriesTableError(ValidationError):
    """Raised for a table or column not on the allowlist.

    A distinct type because the caller's recovery differs from an ordinary
    validation failure: this means the *code* named something that does not
    exist on this path, not that the *data* was bad.
    """


class PostgresTimeSeriesStorage:
    """Append rows to an allowlisted timeseries table using ``COPY``.

    Parameters
    ----------
    engine
        The engine to acquire connections from. An engine rather than a session,
        because this path is deliberately outside the Unit of Work: it writes
        immutable observations that participate in no transactional decision
        (ADR-053, ADR-054). A caller that needs a write to be atomic with a
        domain change is describing an aggregate, and aggregates use a
        repository.
    tables
        Table name to its permitted column names, in the order ``COPY`` will
        send them. Supplying this explicitly rather than reflecting the schema
        keeps the set of things this class can write to a reviewable constant.

    Raises
    ------
    UnknownTimeSeriesTableError
        If the allowlist itself contains an identifier that is not a plain
        lowercase name. Checked at construction so a bad allowlist fails at
        wiring time rather than under load.
    """

    __slots__ = ("_engine", "_tables")

    def __init__(self, engine: AsyncEngine, tables: Mapping[str, Sequence[str]]) -> None:
        for table, columns in tables.items():
            if not _IDENTIFIER.match(table):
                raise UnknownTimeSeriesTableError(
                    "timeseries table name is not a plain identifier", table=table
                )
            for column in columns:
                if not _IDENTIFIER.match(column):
                    raise UnknownTimeSeriesTableError(
                        "timeseries column name is not a plain identifier",
                        table=table,
                        column=column,
                    )
        self._engine = engine
        self._tables = {table: tuple(columns) for table, columns in tables.items()}

    async def append(self, table: str, rows: Sequence[Mapping[str, object]]) -> int:
        """Append ``rows`` to ``table``, returning how many were written.

        Parameters
        ----------
        table
            An allowlisted table name.
        rows
            Column mappings. Every row must supply every allowlisted column for
            the table: a partial row on a bulk path means a silent NULL in a
            column that is usually ``NOT NULL``, discovered thousands of rows
            later.

        Returns
        -------
        int
            ``len(rows)``. An empty sequence is a no-op returning ``0`` rather
            than an error, so a caller draining an empty buffer needs no guard.

        Raises
        ------
        UnknownTimeSeriesTableError
            If ``table`` is not allowlisted, or a row omits a required column.
        """
        if not rows:
            return 0

        columns = self._resolve_columns(table)
        records = [self._to_record(table, columns, row) for row in rows]

        async with self._engine.begin() as connection:
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection
            if driver is None:  # pragma: no cover - only on a non-asyncpg driver
                raise UnknownTimeSeriesTableError(
                    "the timeseries path requires the asyncpg driver", table=table
                )
            await driver.copy_records_to_table(table, records=records, columns=list(columns))
        return len(rows)

    def _resolve_columns(self, table: str) -> tuple[str, ...]:
        """Return the allowlisted columns for ``table``, or refuse."""
        columns = self._tables.get(table)
        if columns is None:
            raise UnknownTimeSeriesTableError(
                "table is not on the timeseries allowlist",
                table=table,
                permitted=sorted(self._tables),
            )
        return columns

    @staticmethod
    def _to_record(
        table: str, columns: tuple[str, ...], row: Mapping[str, object]
    ) -> tuple[object, ...]:
        """Project one mapping into positional order, refusing a partial row."""
        try:
            return tuple(row[column] for column in columns)
        except KeyError as missing:
            raise UnknownTimeSeriesTableError(
                "row is missing a required timeseries column",
                table=table,
                column=str(missing.args[0]),
                required=list(columns),
            ) from missing
