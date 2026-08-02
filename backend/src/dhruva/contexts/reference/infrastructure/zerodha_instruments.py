"""Narrow read-only Kite instrument-master adapter (ADR-076).

Only the documented ``GET /instruments`` surface is represented here. The
official all-capabilities SDK deliberately is not imported, so order, GTT,
portfolio and position operations are structurally absent from this adapter.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import math
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.reference.domain.instrument_master import (
    InstrumentMasterEntry,
    InstrumentMasterSnapshot,
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
    from datetime import date

    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.time.clock import Clock

__all__ = ["KiteInstrumentMasterAdapter", "parse_instrument_master"]

_INSTRUMENTS_PATH = "/instruments"
_KITE_API_HOST = "api.kite.trade"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_ROWS = 250_000
_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 30.0
_HTTP_OK = 200
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX = 599
_REQUIRED_COLUMNS = frozenset(
    {
        "instrument_token",
        "exchange_token",
        "tradingsymbol",
        "name",
        "expiry",
        "strike",
        "tick_size",
        "lot_size",
        "instrument_type",
        "segment",
        "exchange",
    }
)


class KiteInstrumentMasterAdapter:
    """Download and normalize the documented daily Kite CSV dump."""

    __slots__ = ("_access_token", "_api_key", "_client", "_clock", "_sleep")

    def __init__(
        self,
        *,
        client: httpx2.AsyncClient,
        api_key: SecretValue,
        access_token: SecretValue,
        clock: Clock,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind an injected HTTP client, secrets, clock and retry sleeper."""
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
        self._sleep = sleep

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Fetch once per attempt and return a replayable provider-neutral snapshot."""
        content = await self._download()
        fetched_at = self._clock.now()
        entries = parse_instrument_master(content)
        return InstrumentMasterSnapshot(
            provider="zerodha",
            market_date=market_date,
            fetched_at=fetched_at,
            content_sha256=hashlib.sha256(content).hexdigest(),
            raw_csv=content,
            entries=entries,
        )

    async def _download(self) -> bytes:
        """Retry only throttles and transient upstream failures with bounded delay."""
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with self._client.stream(
                    "GET",
                    _INSTRUMENTS_PATH,
                    headers={
                        "X-Kite-Version": "3",
                        "Authorization": (
                            f"token {self._api_key.reveal()}:{self._access_token.reveal()}"
                        ),
                        "Accept": "text/csv",
                    },
                ) as response:
                    content, delay = await _response_result(response, attempt)
            except httpx2.TimeoutException:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise UpstreamTimeoutError(
                        "Kite instrument-master request timed out",
                        endpoint=_INSTRUMENTS_PATH,
                    ) from None
                await self._sleep(_backoff(attempt))
                continue
            except httpx2.TransportError:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise UpstreamUnavailableError(
                        "Kite instrument-master endpoint is unavailable",
                        endpoint=_INSTRUMENTS_PATH,
                    ) from None
                await self._sleep(_backoff(attempt))
                continue

            if content is not None:
                return content
            await self._sleep(delay)

        raise AssertionError("bounded retry loop must return or raise")  # pragma: no cover


def parse_instrument_master(content: bytes) -> tuple[InstrumentMasterEntry, ...]:
    """Parse the documented CSV schema strictly without provider SDK types."""
    if not content:
        raise DataQualityError("instrument master was empty")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise DataQualityError(
            "instrument master exceeded the safe response bound",
            bytes_received=len(content),
            maximum=_MAX_RESPONSE_BYTES,
        )

    row_number = 1
    try:
        stream = TextIOWrapper(BytesIO(content), encoding="utf-8-sig", newline="")
        reader = csv.DictReader(stream)
        columns = frozenset(reader.fieldnames or ())
        missing = _REQUIRED_COLUMNS - columns
        if missing:
            raise DataQualityError(
                "instrument master is missing required columns",
                missing_count=len(missing),
            )
        entries: list[InstrumentMasterEntry] = []
        for row_number, row in enumerate(reader, start=2):
            if row_number > _MAX_ROWS + 1:
                raise DataQualityError(
                    "instrument master exceeded the safe row bound",
                    maximum=_MAX_ROWS,
                )
            entries.append(_entry(row))
    except UnicodeError:
        raise DataQualityError("instrument master is not valid UTF-8 CSV") from None
    except (InvalidOperation, KeyError, TypeError, ValueError, InvariantViolation):
        raise DataQualityError(
            "instrument master contains a malformed row",
            row_number=row_number,
        ) from None

    if not entries:
        raise DataQualityError("instrument master contains no data rows")
    tokens = tuple(entry.instrument_token for entry in entries)
    if len(tokens) != len(set(tokens)):
        raise DataQualityError("instrument master contains duplicate instrument tokens")
    return tuple(entries)


async def _response_result(
    response: httpx2.Response,
    attempt: int,
) -> tuple[bytes | None, float]:
    """Validate a response or return the bounded delay for its next retry."""
    if response.status_code in {401, 403}:
        raise UpstreamAuthenticationError(
            "Kite session is absent, expired, or rejected",
            endpoint=_INSTRUMENTS_PATH,
            status=response.status_code,
        )
    if response.status_code == _HTTP_TOO_MANY_REQUESTS:
        delay = _retry_delay(response.headers.get("Retry-After"), attempt)
        if attempt + 1 == _MAX_ATTEMPTS:
            raise RateLimitedError(
                "Kite instrument-master request remained rate limited",
                endpoint=_INSTRUMENTS_PATH,
                retry_after_seconds=delay,
            )
        return None, delay
    if _HTTP_SERVER_ERROR_MIN <= response.status_code <= _HTTP_SERVER_ERROR_MAX:
        if attempt + 1 == _MAX_ATTEMPTS:
            raise UpstreamUnavailableError(
                "Kite instrument-master endpoint returned a server error",
                endpoint=_INSTRUMENTS_PATH,
                status=response.status_code,
            )
        return None, _backoff(attempt)
    if response.status_code != _HTTP_OK:
        raise ExternalServiceError(
            "Kite instrument-master request was rejected",
            endpoint=_INSTRUMENTS_PATH,
            status=response.status_code,
        )
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > _MAX_RESPONSE_BYTES:
            raise DataQualityError(
                "Kite instrument master exceeded the safe response bound",
                bytes_received=received,
                maximum=_MAX_RESPONSE_BYTES,
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise DataQualityError("Kite instrument master was empty")
    return content, 0.0


def _entry(row: dict[str, str | None]) -> InstrumentMasterEntry:
    """Convert one CSV row using exact decimal and date parsing."""
    from datetime import date  # noqa: PLC0415 - keeps runtime imports local to parsing

    expiry_text = _required(row, "expiry", allow_empty=True)
    return InstrumentMasterEntry(
        instrument_token=int(_required(row, "instrument_token")),
        exchange_token=int(_required(row, "exchange_token")),
        trading_symbol=_required(row, "tradingsymbol"),
        name=_required(row, "name", allow_empty=True),
        expiry=date.fromisoformat(expiry_text) if expiry_text else None,
        strike=Decimal(_required(row, "strike", allow_empty=True) or "0"),
        tick_size=Decimal(_required(row, "tick_size")),
        lot_size=int(_required(row, "lot_size")),
        instrument_type=_required(row, "instrument_type"),
        segment=_required(row, "segment"),
        exchange=_required(row, "exchange"),
    )


def _required(row: dict[str, str | None], field: str, *, allow_empty: bool = False) -> str:
    """Read one non-null CSV field without silently trimming provider data."""
    value = row[field]
    if value is None or (not allow_empty and not value):
        raise ValueError(f"missing {field}")
    return value


def _backoff(attempt: int) -> float:
    """Return a deterministic, bounded exponential retry delay."""
    return 0.25 * (2.0**attempt)


def _retry_delay(value: str | None, attempt: int) -> float:
    """Honor numeric Retry-After values without allowing an unbounded sleep."""
    if value is None:
        return _backoff(attempt)
    try:
        parsed = float(value)
    except ValueError:
        return _backoff(attempt)
    if not math.isfinite(parsed) or parsed < 0:
        return _backoff(attempt)
    return min(parsed, _MAX_RETRY_AFTER_SECONDS)
