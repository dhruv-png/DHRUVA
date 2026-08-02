"""Narrow read-only Kite daily historical-candle adapter (ADR-076)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
)
from dhruva.shared.errors import (
    DataQualityError,
    ExternalServiceError,
    InvariantViolation,
    RateLimitedError,
    UnsafeConfigurationError,
    UpstreamAuthenticationError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.time.clock import Clock

__all__ = ["KiteDailyHistoryAdapter", "KiteHistoryTiming", "parse_daily_history"]

_KITE_API_HOST = "api.kite.trade"
_HISTORY_ENDPOINT = "/instruments/historical/{instrument_token}/day"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_CANDLES = 2_000
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 30.0
_SAFE_REQUEST_INTERVAL = 0.5
_HTTP_OK = 200
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 599
_INDIA_OFFSET = timedelta(hours=5, minutes=30)
_CANDLE_WIDTH = 6
_OI_CANDLE_WIDTH = 7


class KiteHistoryTiming:
    """Serialize requests below 3 req/s and expose injected retry sleeping."""

    __slots__ = ("_last_request", "_lock", "_monotonic", "_sleep")

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

    async def before_request(self) -> None:
        """Wait until a conservative request slot is available."""
        async with self._lock:
            now = self._monotonic()
            if self._last_request is not None:
                delay = self._last_request + _SAFE_REQUEST_INTERVAL - now
                if delay > 0:
                    await self._sleep(delay)
            self._last_request = self._monotonic()

    async def pause(self, delay: float) -> None:
        """Apply an injected retry delay."""
        await self._sleep(delay)


class KiteDailyHistoryAdapter:
    """Fetch only documented daily candles; no broker-capital surface exists."""

    __slots__ = ("_access_token", "_api_key", "_client", "_clock", "_timing")

    def __init__(
        self,
        *,
        client: httpx2.AsyncClient,
        api_key: SecretValue,
        access_token: SecretValue,
        clock: Clock,
        timing: KiteHistoryTiming | None = None,
    ) -> None:
        """Bind secrets, UTC time, a shared rate gate and an injected transport."""
        if client.base_url.scheme != "https" or client.base_url.host != _KITE_API_HOST:
            raise UnsafeConfigurationError(
                "Kite HTTP client must use the official TLS endpoint",
                host=client.base_url.host,
            )
        if client.follow_redirects:
            raise UnsafeConfigurationError("Kite HTTP redirects must remain disabled")
        self._client = client
        self._api_key = api_key
        self._access_token = access_token
        self._clock = clock
        self._timing = timing or KiteHistoryTiming()

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Retrieve one bounded daily range and preserve exact response evidence."""
        content = await self._download(request)
        return parse_daily_history(content, request=request, retrieved_at=self._clock.now())

    async def _download(self, request: DailyHistoryRequest) -> bytes:
        """Retry only throttles and transient upstream failures."""
        path = _HISTORY_ENDPOINT.format(instrument_token=request.source_instrument_id)
        for attempt in range(_MAX_ATTEMPTS):
            await self._timing.before_request()
            try:
                async with self._client.stream(
                    "GET",
                    path,
                    params={
                        "from": request.from_date.isoformat(),
                        "to": request.to_date.isoformat(),
                        "continuous": int(request.continuous),
                        "oi": int(request.include_open_interest),
                    },
                    headers={
                        "X-Kite-Version": "3",
                        "Authorization": (
                            f"token {self._api_key.reveal()}:{self._access_token.reveal()}"
                        ),
                        "Accept": "application/json",
                    },
                ) as response:
                    content, delay = await _response_result(response, attempt)
            except httpx2.TimeoutException:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise UpstreamTimeoutError(
                        "Kite daily-history request timed out",
                        endpoint=_HISTORY_ENDPOINT,
                    ) from None
                await self._timing.pause(_backoff(attempt))
                continue
            except httpx2.TransportError:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise UpstreamUnavailableError(
                        "Kite daily-history endpoint is unavailable",
                        endpoint=_HISTORY_ENDPOINT,
                    ) from None
                await self._timing.pause(_backoff(attempt))
                continue
            if content is not None:
                return content
            await self._timing.pause(delay)
        raise AssertionError("bounded retry loop must return or raise")  # pragma: no cover


def parse_daily_history(
    content: bytes,
    *,
    request: DailyHistoryRequest,
    retrieved_at: datetime,
) -> DailyHistoryBatch:
    """Parse the documented JSON array shape with exact decimal prices."""
    if not content:
        raise DataQualityError("daily history response was empty")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise DataQualityError(
            "daily history response exceeded the safe response bound",
            bytes_received=len(content),
            maximum=_MAX_RESPONSE_BYTES,
        )
    try:
        rows = _payload_rows(json.loads(content, parse_float=Decimal))
        candles = tuple(_candle(row, include_oi=request.include_open_interest) for row in rows)
        return DailyHistoryBatch(
            provider="zerodha",
            request=request,
            retrieved_at=retrieved_at,
            content_sha256=hashlib.sha256(content).hexdigest(),
            raw_response=content,
            adjustment_status=AdjustmentStatus.UNKNOWN,
            candles=candles,
        )
    except DataQualityError:
        raise
    except (InvalidOperation, InvariantViolation, UnicodeError, ValueError, TypeError):
        raise DataQualityError("daily history response is malformed") from None


def _payload_rows(payload: object) -> list[object]:
    """Validate the provider envelope outside the JSON decoder exception boundary."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("unexpected response envelope")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError("missing response data")
    rows = data.get("candles")
    if not isinstance(rows, list) or not rows:
        raise TypeError("missing candles")
    if len(rows) > _MAX_CANDLES:
        raise DataQualityError(
            "daily history response exceeded the safe candle bound",
            maximum=_MAX_CANDLES,
        )
    return rows


def _candle(value: object, *, include_oi: bool) -> DailyCandle:
    """Convert one six- or seven-element provider candle."""
    from datetime import datetime  # noqa: PLC0415 - parser-only dependency

    if not isinstance(value, list) or len(value) not in {_CANDLE_WIDTH, _OI_CANDLE_WIDTH}:
        raise ValueError("invalid candle width")
    if include_oi and len(value) != _OI_CANDLE_WIDTH:
        raise ValueError("open interest was requested but omitted")
    timestamp = value[0]
    if not isinstance(timestamp, str):
        raise TypeError("candle timestamp is not text")
    observed = datetime.fromisoformat(timestamp)
    if observed.tzinfo is None or observed.utcoffset() != _INDIA_OFFSET:
        raise ValueError("daily candle timestamp is not India local time")
    return DailyCandle(
        trading_date=observed.date(),
        open=_decimal(value[1]),
        high=_decimal(value[2]),
        low=_decimal(value[3]),
        close=_decimal(value[4]),
        volume=_integer(value[5]),
        open_interest=_integer(value[6]) if len(value) == _OI_CANDLE_WIDTH else None,
    )


def _decimal(value: object) -> Decimal:
    """Accept JSON integers or exact parsed decimals, never binary floats."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise TypeError("price is not numeric")
    return Decimal(value)


def _integer(value: object) -> int:
    """Require provider quantities to be exact JSON integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("quantity is not an integer")
    return value


async def _response_result(
    response: httpx2.Response,
    attempt: int,
) -> tuple[bytes | None, float]:
    """Validate one response or return a bounded retry delay."""
    if response.status_code in {401, 403}:
        raise UpstreamAuthenticationError(
            "Kite session is absent, expired, or rejected",
            endpoint=_HISTORY_ENDPOINT,
            status=response.status_code,
        )
    if response.status_code == _HTTP_TOO_MANY_REQUESTS:
        delay = _retry_delay(response.headers.get("Retry-After"), attempt)
        if attempt + 1 == _MAX_ATTEMPTS:
            raise RateLimitedError(
                "Kite daily-history request remained rate limited",
                endpoint=_HISTORY_ENDPOINT,
                retry_after_seconds=delay,
            )
        return None, delay
    if _HTTP_SERVER_ERROR_MIN <= response.status_code <= _HTTP_SERVER_ERROR_MAX:
        if attempt + 1 == _MAX_ATTEMPTS:
            raise UpstreamUnavailableError(
                "Kite daily-history endpoint returned a server error",
                endpoint=_HISTORY_ENDPOINT,
                status=response.status_code,
            )
        return None, _backoff(attempt)
    if response.status_code != _HTTP_OK:
        raise ExternalServiceError(
            "Kite daily-history request was rejected",
            endpoint=_HISTORY_ENDPOINT,
            status=response.status_code,
        )
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > _MAX_RESPONSE_BYTES:
            raise DataQualityError(
                "Kite daily history exceeded the safe response bound",
                bytes_received=received,
                maximum=_MAX_RESPONSE_BYTES,
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise DataQualityError("Kite daily history was empty")
    return content, 0.0


def _backoff(attempt: int) -> float:
    """Return a deterministic, bounded exponential retry delay."""
    return 0.25 * (2.0**attempt)


def _retry_delay(value: str | None, attempt: int) -> float:
    """Honor finite numeric Retry-After values without unbounded sleeping."""
    if value is None:
        return _backoff(attempt)
    try:
        parsed = float(value)
    except ValueError:
        return _backoff(attempt)
    if not math.isfinite(parsed) or parsed < 0:
        return _backoff(attempt)
    return min(parsed, _MAX_RETRY_AFTER_SECONDS)
