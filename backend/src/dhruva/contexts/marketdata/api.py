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
    GetContinuousFuturesSeries,
    GetContinuousFuturesSeriesQuery,
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
    GetMarketContext,
    GetMarketContextQuery,
    contexts_by_instrument,
    contract_terms,
)
from dhruva.contexts.marketdata.domain import (
    CONTINUOUS_FUTURES_POLICY_REVISION,
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    MARKET_CONTEXT_REVISION,
    MAX_MULTI_DAY_SESSIONS,
    AdjustmentStatus,
    BarCompleteness,
    ContinuousBar,
    ContinuousFuturesSeries,
    DailyBarRevision,
    DailyBarSeries,
    DailyHistoryRequest,
    ExecutionContract,
    FuturesContractTerms,
    FuturesRoll,
    MarketContext,
    MarketDataAvailability,
    MarketInstrumentKind,
    RollPolicy,
    RollReason,
)

__all__ = [
    "CONTINUOUS_FUTURES_POLICY_REVISION",
    "DAILY_HISTORY_QUALITY_REVISION",
    "DEFAULT_MULTI_DAY_SESSIONS",
    "DEFAULT_STALE_AFTER_DAYS",
    "MARKET_CONTEXT_REVISION",
    "MAX_MULTI_DAY_SESSIONS",
    "AdjustmentStatus",
    "BarCompleteness",
    "ContinuousBar",
    "ContinuousFuturesSeries",
    "DailyBarRevision",
    "DailyBarSeries",
    "DailyHistoryRequest",
    "ExecutionContract",
    "FuturesContractTerms",
    "FuturesRoll",
    "GetContinuousFuturesSeries",
    "GetContinuousFuturesSeriesQuery",
    "GetDailyBarSeries",
    "GetDailyBarSeriesQuery",
    "GetMarketContext",
    "GetMarketContextQuery",
    "MarketContext",
    "MarketDataAvailability",
    "MarketInstrumentKind",
    "RollPolicy",
    "RollReason",
    "contexts_by_instrument",
    "contract_terms",
]
