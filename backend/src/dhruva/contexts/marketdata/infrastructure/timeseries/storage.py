"""Transaction-bound PostgreSQL append path for daily bar primitives (ADR-054)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from dhruva.contexts.marketdata.infrastructure.persistence.records import DailyBarRecord

__all__ = ["DailyBarStorage"]


class DailyBarStorage:
    """Bulk-insert immutable primitive rows without importing a domain entity."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind the append path to its caller-owned transaction."""
        self._session = session

    async def append(self, records: tuple[DailyBarRecord, ...]) -> frozenset[UUID]:
        """Insert all unseen record ids and return exactly those added."""
        if not records:
            return frozenset()
        values = [
            {field: getattr(record, field) for field in record.__dataclass_fields__}
            for record in records
        ]
        result = await self._session.execute(
            insert(DailyMarketBarRevisionModel)
            .values(values)
            .on_conflict_do_nothing(index_elements=[DailyMarketBarRevisionModel.id])
            .returning(DailyMarketBarRevisionModel.id)
        )
        return frozenset(result.scalars().all())
