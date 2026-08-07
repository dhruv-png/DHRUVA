"""Domain layer.

Entities, value objects, domain services, domain events and PORTS.

This layer performs no I/O and imports no framework. It may import only
``dhruva.shared`` and the standard library. Enforced by ADR-001 layering
and the import-linter contract ``Clean Architecture layers``.
"""

from dhruva.contexts.marketdata.domain.continuous_futures import (
    CONTINUOUS_FUTURES_POLICY_REVISION,
    ContinuousBar,
    ContinuousFuturesSeries,
    ExecutionContract,
    FuturesContractTerms,
    FuturesRoll,
    RollPolicy,
    RollReason,
    build_continuous_series,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarArchiveWrite,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.domain.market_context import (
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    MARKET_CONTEXT_REVISION,
    MAX_MULTI_DAY_SESSIONS,
    MarketContext,
    MarketDataAvailability,
    absent_context,
    summarise_recent_bars,
)

__all__ = [
    "CONTINUOUS_FUTURES_POLICY_REVISION",
    "DEFAULT_MULTI_DAY_SESSIONS",
    "DEFAULT_STALE_AFTER_DAYS",
    "MARKET_CONTEXT_REVISION",
    "MAX_MULTI_DAY_SESSIONS",
    "AdjustmentStatus",
    "BarCompleteness",
    "ContinuousBar",
    "ContinuousFuturesSeries",
    "DailyBarArchiveWrite",
    "DailyBarRevision",
    "DailyBarSeries",
    "DailyCandle",
    "DailyHistoryBatch",
    "DailyHistoryRequest",
    "ExecutionContract",
    "FuturesContractTerms",
    "FuturesRoll",
    "MarketContext",
    "MarketDataAvailability",
    "MarketInstrumentKind",
    "RollPolicy",
    "RollReason",
    "absent_context",
    "build_continuous_series",
    "summarise_recent_bars",
]
