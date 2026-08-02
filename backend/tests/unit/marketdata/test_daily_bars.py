"""Daily bar invariants reject plausible-looking corrupt market data."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

INSTRUMENT = InstrumentId.deterministic("fixture", "cash")
REQUEST = DailyHistoryRequest(
    instrument_id=INSTRUMENT,
    instrument_kind=MarketInstrumentKind.CASH_EQUITY,
    source_instrument_id=12345,
    from_date=date(2026, 7, 29),
    to_date=date(2026, 7, 31),
)
RAW = b'{"fixture":true}'


def _candle(*, trading_date: date = date(2026, 7, 29)) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=1000,
        open_interest=None,
    )


def _batch(candles: tuple[DailyCandle, ...]) -> DailyHistoryBatch:
    return DailyHistoryBatch(
        provider="zerodha",
        request=REQUEST,
        retrieved_at=datetime(2026, 8, 2, 6, tzinfo=UTC),
        content_sha256=hashlib.sha256(RAW).hexdigest(),
        raw_response=RAW,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        candles=candles,
    )


def _bar(
    trading_date: date,
    *,
    adjustment_status: AdjustmentStatus,
) -> DailyBarRevision:
    return DailyBarRevision(
        instrument_id=INSTRUMENT,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="zerodha",
        source_instrument_id=12345,
        candle=_candle(trading_date=trading_date),
        retrieved_at=datetime(2026, 8, 2, 6, tzinfo=UTC),
        adjustment_status=adjustment_status,
        completeness=BarCompleteness.COMPLETE,
        source_revision="1" * 64,
        batch_sha256="2" * 64,
        quality_revision="daily-history-quality-v1",
    )


def test_invalid_ohlcv_is_refused() -> None:
    """Prices must be positive, ordered and accompanied by non-negative volume."""
    with pytest.raises(InvariantViolation):
        replace(_candle(), open=Decimal("0"))
    with pytest.raises(InvariantViolation):
        replace(_candle(), close=Decimal("-1"))
    with pytest.raises(InvariantViolation):
        replace(_candle(), high=Decimal("98"))
    with pytest.raises(InvariantViolation):
        replace(_candle(), low=Decimal("106"))
    with pytest.raises(InvariantViolation):
        replace(_candle(), volume=-1)


def test_duplicate_or_unsorted_dates_are_refused() -> None:
    """One provider response cannot silently overwrite a date or reverse time."""
    first = _candle(trading_date=date(2026, 7, 29))
    second = _candle(trading_date=date(2026, 7, 30))

    with pytest.raises(InvariantViolation, match="sorted"):
        _batch((second, first))
    with pytest.raises(InvariantViolation, match="unique"):
        _batch((first, first))


def test_cash_history_cannot_request_continuous_or_open_interest() -> None:
    """Futures-only provider flags cannot leak into a cash request."""
    with pytest.raises(InvariantViolation, match="futures-only"):
        replace(REQUEST, continuous=True)
    with pytest.raises(InvariantViolation, match="futures-only"):
        replace(REQUEST, include_open_interest=True)


def test_digest_and_requested_range_are_enforced() -> None:
    """Response evidence must match its digest and requested date window."""
    with pytest.raises(InvariantViolation, match="digest"):
        replace(_batch((_candle(),)), content_sha256="0" * 64)
    with pytest.raises(InvariantViolation, match="outside"):
        _batch((_candle(trading_date=date(2026, 8, 1)),))


def test_adjusted_and_unknown_bars_cannot_form_one_series() -> None:
    """A backtest-facing series cannot mix incompatible adjustment semantics."""
    with pytest.raises(InvariantViolation, match="adjustment"):
        DailyBarSeries(
            bars=(
                _bar(date(2026, 7, 29), adjustment_status=AdjustmentStatus.UNKNOWN),
                _bar(date(2026, 7, 30), adjustment_status=AdjustmentStatus.ADJUSTED),
            )
        )
