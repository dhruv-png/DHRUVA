"""The SQLAlchemy repository for the worked example aggregate.

Demonstrates the pattern every real repository follows. It **orchestrates**
reconstruction rather than performing it (Amendment 2): the model comes from the
session, the mapper flattens it to a record, and the factory -- which holds the
calendar -- turns that into an aggregate.

Three properties are load-bearing:

* **It never commits.** Transaction lifetime belongs to the Unit of Work
  (ADR-053). Nothing here calls ``commit``, ``rollback`` or ``begin``.
* **Updates are explicit and version-checked.** There is no dirty tracking to
  fall back on (ADR-057), and a lost update raises rather than being overwritten.
* **No SQLAlchemy type escapes.** Callers receive domain objects; models and
  records stay inside this layer (ADR-052).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from sqlalchemy import CursorResult, select, update

from dhruva.contexts.platform.infrastructure.persistence.factories import DailySnapshotFactory
from dhruva.contexts.platform.infrastructure.persistence.mappers import (
    to_model_kwargs,
    to_record,
)
from dhruva.contexts.platform.infrastructure.persistence.models import DailySnapshotModel
from dhruva.shared.errors import ConflictError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.platform.domain.example_snapshot import DailySnapshot
    from dhruva.shared.identity import AccountId, InstrumentId
    from dhruva.shared.time import TradingDay

__all__ = ["DailySnapshotRepository"]


class DailySnapshotRepository:
    """Loads and stores :class:`DailySnapshot` aggregates."""

    __slots__ = ("_factory", "_session")

    def __init__(self, session: AsyncSession, factory: DailySnapshotFactory) -> None:
        """Bind the repository to a session and a reconstruction factory.

        Both are injected. The repository constructs neither, which is what
        keeps it focused on persistence rather than on assembling its own
        dependencies.
        """
        self._session = session
        self._factory = factory

    async def get(
        self, account_id: AccountId, instrument_id: InstrumentId, trading_day: TradingDay
    ) -> DailySnapshot | None:
        """Return the snapshot for a natural key, or ``None`` if absent.

        ``None`` rather than raising: "not found" is an ordinary answer to a
        lookup, and a caller requiring existence raises its own
        :class:`~dhruva.shared.errors.NotFoundError` with context this layer does
        not have.
        """
        statement = select(DailySnapshotModel).where(
            DailySnapshotModel.account_id == account_id.value,
            DailySnapshotModel.instrument_id == instrument_id.value,
            DailySnapshotModel.trading_day == trading_day.on,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            return None
        return self._factory.reconstruct(to_record(model))

    async def add(self, aggregate: DailySnapshot) -> None:
        """Stage a new snapshot for insertion.

        Staged, not written: nothing reaches the database until the Unit of Work
        commits. The surrogate row identifier is minted here because it is a
        persistence concern -- the domain identifies a snapshot by instrument and
        trading day and knows nothing about the row it occupies.
        """
        record = self._factory.deconstruct(aggregate, uuid4())
        self._session.add(DailySnapshotModel(**to_model_kwargs(record)))

    async def update(self, aggregate: DailySnapshot) -> None:
        """Stage changes, refusing the write if another writer got there first.

        The aggregate carries the version it was loaded at. The UPDATE matches on
        that version and writes ``version + 1``; zero rows affected means someone
        else committed in between.

        Raises
        ------
        ConflictError
            If the stored version no longer matches. Retry policy belongs to the
            caller -- the repository reports the conflict and takes no view on
            whether retrying is safe, because only the use case knows whether the
            mutation is idempotent.
        """
        previous_version = aggregate.version - 1
        record = self._factory.deconstruct(aggregate, uuid4())
        statement = (
            update(DailySnapshotModel)
            .where(
                DailySnapshotModel.account_id == record.account_id,
                DailySnapshotModel.instrument_id == record.instrument_id,
                DailySnapshotModel.trading_day == record.trading_day,
                DailySnapshotModel.version == previous_version,
            )
            .values(
                close_scaled_units=record.close_scaled_units,
                turnover_minor_units=record.turnover_minor_units,
                currency=record.currency,
                version=aggregate.version,
            )
        )
        # `rowcount` lives on CursorResult; `execute` is typed as returning the
        # broader Result. The cast is a typing accommodation, not a behavioural
        # assumption -- an UPDATE always yields a cursor result.
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        if result.rowcount == 0:
            msg = "snapshot was modified by another writer"
            raise ConflictError(
                msg,
                instrument_id=str(aggregate.instrument_id),
                trading_day=aggregate.trading_day.iso,
                expected_version=previous_version,
            )
