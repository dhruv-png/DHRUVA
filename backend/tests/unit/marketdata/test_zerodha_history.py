"""Kite daily history adapter contract against sanitized JSON fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyHistoryRequest,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.infrastructure.zerodha_history import (
    KiteDailyHistoryAdapter,
    KiteHistoryTiming,
    parse_daily_history,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import (
    DataQualityError,
    RateLimitedError,
    UnsafeConfigurationError,
    UpstreamAuthenticationError,
)
from dhruva.shared.identity import InstrumentId
from dhruva.shared.time.clock import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[2] / "fixtures" / "zerodha" / "daily_history_cash_sanitized.json"
NOW = datetime(2026, 8, 2, 6, tzinfo=UTC)
REQUEST = DailyHistoryRequest(
    instrument_id=InstrumentId.deterministic("fixture", "cash"),
    instrument_kind=MarketInstrumentKind.CASH_EQUITY,
    source_instrument_id=12345,
    from_date=date(2026, 7, 29),
    to_date=date(2026, 7, 31),
)


async def _no_sleep(delay: float) -> None:
    assert delay >= 0


def _timing() -> KiteHistoryTiming:
    ticks = iter((0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0))
    return KiteHistoryTiming(monotonic=lambda: next(ticks), sleep=_no_sleep)


def _adapter(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    timing: KiteHistoryTiming | None = None,
) -> KiteDailyHistoryAdapter:
    transport = httpx2.MockTransport(handler)
    client = httpx2.AsyncClient(
        base_url="https://api.kite.trade",
        transport=transport,
        follow_redirects=False,
    )
    return KiteDailyHistoryAdapter(
        client=client,
        api_key=SecretValue("fixture-api-key"),
        access_token=SecretValue("fixture-access-token"),
        clock=FrozenClock(NOW),
        timing=timing or _timing(),
    )


def test_fixture_parses_exact_prices_and_unknown_adjustment() -> None:
    """Kite history is not silently labelled raw or adjusted."""
    batch = parse_daily_history(FIXTURE.read_bytes(), request=REQUEST, retrieved_at=NOW)

    assert len(batch.candles) == 3
    assert batch.candles[0].open == Decimal("100.00")
    assert batch.candles[-1].close == Decimal("105.50")
    assert batch.adjustment_status is AdjustmentStatus.UNKNOWN
    assert all(item.open_interest is None for item in batch.candles)


@pytest.mark.asyncio
async def test_adapter_uses_only_documented_daily_read_parameters() -> None:
    """The request is GET-only, bounded, authenticated and explicitly non-continuous."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=FIXTURE.read_bytes())

    batch = await _adapter(handler).fetch(REQUEST)

    sent = requests[0]
    assert sent.method == "GET"
    assert sent.url.path == "/instruments/historical/12345/day"
    assert sent.url.params["from"] == "2026-07-29"
    assert sent.url.params["to"] == "2026-07-31"
    assert sent.url.params["continuous"] == "0"
    assert sent.url.params["oi"] == "0"
    assert sent.headers["X-Kite-Version"] == "3"
    assert batch.retrieved_at == NOW


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_expired_session_is_distinct_and_never_retried(status: int) -> None:
    """Authentication recovery differs from throttling and transient retries."""
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx2.Response(status)

    with pytest.raises(UpstreamAuthenticationError):
        await _adapter(handler).fetch(REQUEST)
    assert calls == 1


@pytest.mark.asyncio
async def test_rate_limit_retries_are_bounded() -> None:
    """Three 429 responses fail visibly without an unbounded retry loop."""
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx2.Response(429, headers={"Retry-After": "999"})

    with pytest.raises(RateLimitedError):
        await _adapter(handler).fetch(REQUEST)
    assert calls == 3


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"status":"success","data":{"candles":[]}}',
        b'{"status":"success","data":{"candles":[["2026-07-29T00:00:00+0530",0,1,1,1,1]]}}',
        b'{"status":"success","data":{"candles":[["2026-07-29T00:00:00Z",1,1,1,1,1]]}}',
        b'{"status":"success","data":{"candles":[["2026-07-29T00:00:00+0530",1,1,1,1,-1]]}}',
    ],
)
def test_malformed_or_impossible_provider_rows_fail_closed(payload: bytes) -> None:
    """Schema drift and plausible corrupt values never become bars."""
    with pytest.raises(DataQualityError):
        parse_daily_history(payload, request=REQUEST, retrieved_at=NOW)


@pytest.mark.asyncio
async def test_non_official_or_redirecting_clients_are_refused() -> None:
    """Credentials cannot be redirected or sent to an unapproved host."""
    for base_url, redirects in (
        ("https://example.invalid", False),
        ("https://api.kite.trade", True),
    ):
        client = httpx2.AsyncClient(base_url=base_url, follow_redirects=redirects)
        try:
            with pytest.raises(UnsafeConfigurationError):
                KiteDailyHistoryAdapter(
                    client=client,
                    api_key=SecretValue("fixture-api-key"),
                    access_token=SecretValue("fixture-access-token"),
                    clock=FrozenClock(NOW),
                )
        finally:
            await client.aclose()
