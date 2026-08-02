"""Provider-neutral daily OHLCV facts and append-only revisions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date, datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "AdjustmentStatus",
    "BarCompleteness",
    "DailyBarArchiveWrite",
    "DailyBarRevision",
    "DailyBarSeries",
    "DailyCandle",
    "DailyHistoryBatch",
    "DailyHistoryRequest",
    "MarketInstrumentKind",
]

_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{1,31}\Z")
_QUALITY_REVISION = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")
_MAX_DAILY_HISTORY_RANGE = 1900


def _utc(value: datetime, *, field: str) -> None:
    invariant(value.tzinfo is not None and value.utcoffset() is not None, f"{field} must be aware")
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")


def _positive_price(value: Decimal, *, field: str) -> None:
    invariant(value.is_finite(), f"{field} must be finite")
    invariant(value > 0, f"{field} must be positive")


class MarketInstrumentKind(StrEnum):
    """Supported persisted daily-series identities."""

    CASH_EQUITY = "CASH_EQUITY"
    INDEX = "INDEX"
    FUTURES_CONTRACT = "FUTURES_CONTRACT"


class AdjustmentStatus(StrEnum):
    """What is proven about corporate-action adjustment of one source series."""

    RAW = "RAW"
    ADJUSTED = "ADJUSTED"
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class BarCompleteness(StrEnum):
    """Whether a daily bar was complete at its retrieval instant."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class DailyHistoryRequest:
    """Bounded provider-history request for one stable instrument identity."""

    instrument_id: InstrumentId
    instrument_kind: MarketInstrumentKind
    source_instrument_id: int
    from_date: date
    to_date: date
    continuous: bool = False
    include_open_interest: bool = False

    def __post_init__(self) -> None:
        """Reject reversed, excessive or semantically incompatible requests."""
        invariant(self.source_instrument_id > 0, "source instrument id must be positive")
        invariant(self.from_date <= self.to_date, "history date range is reversed")
        invariant(
            (self.to_date - self.from_date).days <= _MAX_DAILY_HISTORY_RANGE,
            "daily history request exceeds the safe provider range",
        )
        if self.instrument_kind is not MarketInstrumentKind.FUTURES_CONTRACT:
            invariant(not self.continuous, "continuous history is futures-only")
            invariant(not self.include_open_interest, "open interest is futures-only")


@dataclass(frozen=True, slots=True)
class DailyCandle:
    """One exact daily provider candle before persistence provenance is attached."""

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None

    def __post_init__(self) -> None:
        """Reject impossible OHLC relationships and invalid quantities."""
        _positive_price(self.open, field="open")
        _positive_price(self.high, field="high")
        _positive_price(self.low, field="low")
        _positive_price(self.close, field="close")
        invariant(self.high >= max(self.open, self.close, self.low), "high is below OHLC")
        invariant(self.low <= min(self.open, self.close, self.high), "low is above OHLC")
        invariant(type(self.volume) is int and self.volume >= 0, "volume cannot be negative")
        invariant(
            self.open_interest is None
            or (type(self.open_interest) is int and self.open_interest >= 0),
            "open interest cannot be negative",
        )


@dataclass(frozen=True, slots=True)
class DailyHistoryBatch:
    """Replayable response from one bounded daily-history request."""

    provider: str
    request: DailyHistoryRequest
    retrieved_at: datetime
    content_sha256: str
    raw_response: bytes
    adjustment_status: AdjustmentStatus
    candles: tuple[DailyCandle, ...]

    def __post_init__(self) -> None:
        """Require trustworthy content provenance and deterministic candle order."""
        invariant(bool(_PROVIDER.fullmatch(self.provider)), "invalid market-data provider")
        _utc(self.retrieved_at, field="retrieved_at")
        invariant(bool(_HEX_SHA256.fullmatch(self.content_sha256)), "invalid response SHA-256")
        invariant(bool(self.raw_response), "daily history response must not be empty")
        invariant(
            hashlib.sha256(self.raw_response).hexdigest() == self.content_sha256,
            "daily history digest does not match its bytes",
        )
        invariant(bool(self.candles), "daily history response contains no candles")
        dates = tuple(candle.trading_date for candle in self.candles)
        invariant(dates == tuple(sorted(dates)), "daily candles must be sorted")
        invariant(len(dates) == len(set(dates)), "daily candle dates must be unique")
        invariant(
            all(self.request.from_date <= item <= self.request.to_date for item in dates),
            "daily candle falls outside the requested range",
        )
        if self.request.include_open_interest:
            invariant(
                all(candle.open_interest is not None for candle in self.candles),
                "requested open interest is missing",
            )


@dataclass(frozen=True, slots=True)
class DailyBarRevision:
    """One point-in-knowledge-time persisted daily bar revision."""

    instrument_id: InstrumentId
    instrument_kind: MarketInstrumentKind
    source: str
    source_instrument_id: int
    candle: DailyCandle
    retrieved_at: datetime
    adjustment_status: AdjustmentStatus
    completeness: BarCompleteness
    source_revision: str
    batch_sha256: str
    quality_revision: str

    def __post_init__(self) -> None:
        """Require complete immutable provenance for a stored observation."""
        invariant(bool(_PROVIDER.fullmatch(self.source)), "invalid market-data source")
        invariant(self.source_instrument_id > 0, "source instrument id must be positive")
        _utc(self.retrieved_at, field="retrieved_at")
        invariant(bool(_HEX_SHA256.fullmatch(self.source_revision)), "invalid bar revision")
        invariant(bool(_HEX_SHA256.fullmatch(self.batch_sha256)), "invalid batch SHA-256")
        invariant(
            bool(_QUALITY_REVISION.fullmatch(self.quality_revision)),
            "invalid daily bar quality revision",
        )


@dataclass(frozen=True, slots=True)
class DailyBarSeries:
    """One compatible instrument series at a point in knowledge time."""

    bars: tuple[DailyBarRevision, ...]

    def __post_init__(self) -> None:
        """Prevent mixed instruments, sources or adjustment semantics."""
        invariant(bool(self.bars), "daily bar series must not be empty")
        dates = tuple(item.candle.trading_date for item in self.bars)
        invariant(dates == tuple(sorted(dates)), "daily bar series must be sorted")
        invariant(len(dates) == len(set(dates)), "daily bar series dates must be unique")
        first = self.bars[0]
        invariant(
            all(item.instrument_id == first.instrument_id for item in self.bars),
            "daily bar series mixes instruments",
        )
        invariant(
            all(item.instrument_kind is first.instrument_kind for item in self.bars),
            "daily bar series mixes instrument kinds",
        )
        invariant(
            all(item.source == first.source for item in self.bars),
            "daily bar series mixes providers",
        )
        invariant(
            all(item.adjustment_status is first.adjustment_status for item in self.bars),
            "daily bar series mixes adjustment states",
        )
        invariant(
            all(item.quality_revision == first.quality_revision for item in self.bars),
            "daily bar series mixes quality revisions",
        )


@dataclass(frozen=True, slots=True)
class DailyBarArchiveWrite:
    """Counts distinguishing appended bar revisions from identical retries."""

    added: int
    unchanged: int

    def __post_init__(self) -> None:
        """Keep repository outcomes non-negative."""
        invariant(self.added >= 0, "added bar count cannot be negative")
        invariant(self.unchanged >= 0, "unchanged bar count cannot be negative")
