"""Point-in-time daily-bar repository with a primitive bulk write path."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from sqlalchemy import select

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarArchiveWrite,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)
from dhruva.contexts.marketdata.infrastructure.persistence.records import DailyBarRecord
from dhruva.contexts.marketdata.infrastructure.timeseries.storage import DailyBarStorage
from dhruva.shared.errors import ConflictError, DataQualityError, MissingDataError, ValidationError
from dhruva.shared.identity import InstrumentId

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["DailyBarRepository"]

_BAR_NAMESPACE = UUID("38eb746a-918f-454f-ad27-dd834db51581")


class DailyBarRepository:
    """Append revisions and resolve the latest revision knowable at a cutoff."""

    __slots__ = ("_session", "_storage")

    def __init__(self, session: AsyncSession) -> None:
        """Bind repository and primitive storage to one transaction."""
        self._session = session
        self._storage = DailyBarStorage(session)

    async def add_series(self, series: DailyBarSeries) -> DailyBarArchiveWrite:
        """Stage every new revision and verify all idempotent collisions."""
        records = tuple(_record(bar) for bar in series.bars)
        inserted = await self._storage.append(records)
        unchanged_records = tuple(record for record in records if record.id not in inserted)
        existing_by_id = {
            model.id: model
            for model in (
                (
                    await self._session.execute(
                        select(DailyMarketBarRevisionModel).where(
                            DailyMarketBarRevisionModel.id.in_(
                                tuple(record.id for record in unchanged_records)
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        for record in unchanged_records:
            existing = existing_by_id.get(record.id)
            if existing is None or not _same_record(_model_record(existing), record):
                raise ConflictError(
                    "daily bar revision was reused with different content",
                    instrument_id=str(record.instrument_id),
                    trading_date=record.trading_date.isoformat(),
                    source=record.source,
                )
        return DailyBarArchiveWrite(
            added=len(inserted),
            unchanged=len(records) - len(inserted),
        )

    async def list_series(
        self,
        instrument_id: InstrumentId,
        *,
        from_date: date,
        to_date: date,
        known_at: datetime,
        require_complete: bool,
    ) -> DailyBarSeries:
        """Resolve latest retrieval per date without future corrections."""
        if from_date > to_date:
            raise ValidationError("daily bar query date range is reversed")
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        models = (
            (
                await self._session.execute(
                    select(DailyMarketBarRevisionModel).where(
                        DailyMarketBarRevisionModel.instrument_id == instrument_id.value,
                        DailyMarketBarRevisionModel.trading_date >= from_date,
                        DailyMarketBarRevisionModel.trading_date <= to_date,
                        DailyMarketBarRevisionModel.retrieved_at <= known_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        latest: dict[date, DailyMarketBarRevisionModel] = {}
        for model in models:
            prior = latest.get(model.trading_date)
            if prior is None or (model.retrieved_at, model.id.int) > (
                prior.retrieved_at,
                prior.id.int,
            ):
                latest[model.trading_date] = model
        if not latest:
            raise MissingDataError(
                "daily bar series was not found",
                instrument_id=str(instrument_id),
            )
        bars = tuple(_domain(latest[item]) for item in sorted(latest))
        if require_complete and any(
            item.completeness is BarCompleteness.INCOMPLETE for item in bars
        ):
            raise DataQualityError(
                "daily bar series contains an incomplete revision",
                instrument_id=str(instrument_id),
            )
        adjustments = {item.adjustment_status for item in bars}
        if len(adjustments) != 1:
            raise DataQualityError(
                "daily bar series mixes adjustment states",
                instrument_id=str(instrument_id),
            )
        return DailyBarSeries(bars=bars)


def _record(bar: DailyBarRevision) -> DailyBarRecord:
    """Decompose a validated domain revision into primitive columns."""
    record_id = uuid5(
        _BAR_NAMESPACE,
        "\x1f".join(
            (
                str(bar.instrument_id.value),
                bar.candle.trading_date.isoformat(),
                bar.source,
                bar.source_revision,
            )
        ),
    )
    return DailyBarRecord(
        id=record_id,
        instrument_id=bar.instrument_id.value,
        instrument_kind=bar.instrument_kind.value,
        trading_date=bar.candle.trading_date,
        open_price=bar.candle.open,
        high_price=bar.candle.high,
        low_price=bar.candle.low,
        close_price=bar.candle.close,
        volume=bar.candle.volume,
        open_interest=bar.candle.open_interest,
        source=bar.source,
        source_instrument_id=bar.source_instrument_id,
        retrieved_at=bar.retrieved_at,
        adjustment_status=bar.adjustment_status.value,
        completeness=bar.completeness.value,
        source_revision=bar.source_revision,
        batch_sha256=bar.batch_sha256,
        quality_revision=bar.quality_revision,
    )


def _model_record(model: DailyMarketBarRevisionModel) -> DailyBarRecord:
    """Copy a SQLAlchemy row into an immutable primitive record."""
    return DailyBarRecord(
        id=model.id,
        instrument_id=model.instrument_id,
        instrument_kind=model.instrument_kind,
        trading_date=model.trading_date,
        open_price=model.open_price,
        high_price=model.high_price,
        low_price=model.low_price,
        close_price=model.close_price,
        volume=model.volume,
        open_interest=model.open_interest,
        source=model.source,
        source_instrument_id=model.source_instrument_id,
        retrieved_at=model.retrieved_at,
        adjustment_status=model.adjustment_status,
        completeness=model.completeness,
        source_revision=model.source_revision,
        batch_sha256=model.batch_sha256,
        quality_revision=model.quality_revision,
    )


def _same_record(existing: DailyBarRecord, candidate: DailyBarRecord) -> bool:
    """Ignore retrieval-envelope fields for a semantically identical bar retry."""
    return (
        replace(
            existing,
            retrieved_at=candidate.retrieved_at,
            batch_sha256=candidate.batch_sha256,
            source_instrument_id=candidate.source_instrument_id,
        )
        == candidate
    )


def _domain(model: DailyMarketBarRevisionModel) -> DailyBarRevision:
    """Reconstruct and revalidate one persisted daily bar revision."""
    return DailyBarRevision(
        instrument_id=InstrumentId(model.instrument_id),
        instrument_kind=MarketInstrumentKind(model.instrument_kind),
        source=model.source,
        source_instrument_id=model.source_instrument_id,
        candle=DailyCandle(
            trading_date=model.trading_date,
            open=model.open_price,
            high=model.high_price,
            low=model.low_price,
            close=model.close_price,
            volume=model.volume,
            open_interest=model.open_interest,
        ),
        retrieved_at=model.retrieved_at,
        adjustment_status=AdjustmentStatus(model.adjustment_status),
        completeness=BarCompleteness(model.completeness),
        source_revision=model.source_revision,
        batch_sha256=model.batch_sha256,
        quality_revision=model.quality_revision,
    )
