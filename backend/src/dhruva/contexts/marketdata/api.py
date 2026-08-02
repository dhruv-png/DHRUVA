"""Public API of the C2 ``marketdata`` context.

This module is the **only** import surface other contexts may use. Importing
``dhruva.contexts.marketdata.domain``, ``.application``, ``.infrastructure`` or
``.interfaces`` from another context is a build failure.

Re-export here the DTOs and application services other contexts may use. Kite
transport and PostgreSQL adapters remain internal.
"""

from __future__ import annotations

from dhruva.contexts.marketdata.application import (
    DAILY_HISTORY_QUALITY_REVISION,
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
)
from dhruva.contexts.marketdata.domain import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyHistoryRequest,
    MarketInstrumentKind,
)

__all__ = [
    "DAILY_HISTORY_QUALITY_REVISION",
    "AdjustmentStatus",
    "BarCompleteness",
    "DailyBarRevision",
    "DailyBarSeries",
    "DailyHistoryRequest",
    "GetDailyBarSeries",
    "GetDailyBarSeriesQuery",
    "MarketInstrumentKind",
]
