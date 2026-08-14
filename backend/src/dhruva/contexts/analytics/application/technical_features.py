"""Translate PIT-resolved market-data series into analytics-owned inputs."""

from __future__ import annotations

from dhruva.contexts.analytics.domain.technical import (
    AdjustmentEvidence,
    TechnicalBar,
    TechnicalSeries,
)
from dhruva.contexts.marketdata.api import DailyBarSeries

__all__ = ["technical_series_from_daily_bars"]


def technical_series_from_daily_bars(series: DailyBarSeries) -> TechnicalSeries:
    """Copy a resolved market-data DTO without losing its adjustment evidence."""
    first = series.bars[0]
    return TechnicalSeries(
        instrument_id=first.instrument_id,
        bars=tuple(
            TechnicalBar(
                trading_date=item.candle.trading_date,
                open=item.candle.open,
                high=item.candle.high,
                low=item.candle.low,
                close=item.candle.close,
                volume=item.candle.volume,
            )
            for item in series.bars
        ),
        adjustment_status=AdjustmentEvidence(first.adjustment_status.value),
    )
