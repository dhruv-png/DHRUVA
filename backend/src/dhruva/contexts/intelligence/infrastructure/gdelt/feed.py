"""Narrow read-only GDELT DOC 2.0 transport.

Only the documented `mode=artlist&format=json` endpoint is called, anonymously,
with a bounded retry budget. Nothing here parses: the bytes go straight to the
pure mapper beside it, so every operational outcome is decided in one place and
every payload outcome in another.

DHRUVA never fetches the article itself. GDELT returns the publisher's URL and
DHRUVA stores it for a reader to follow; crawling it would be a different act
under a different set of terms, and this adapter has no code that could.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.intelligence.domain.sources import (
    NewsFetchResult,
    SourceHealth,
    SourceStatus,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    MAX_PAYLOAD_BYTES,
    gdelt_source,
    map_artlist,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from dhruva.shared.time.clock import Clock

__all__ = ["GDELT_DOC_ENDPOINT", "GdeltDocFeed", "GdeltQuery", "GdeltRequestTiming"]

#: The documented DOC 2.0 endpoint. No key, no session, no cookie.
GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

_MAX_ATTEMPTS = 3
_MAX_RETRY_AFTER_SECONDS = 60.0
_BACKOFF_BASE_SECONDS = 1.0
_SERVER_ERROR_MIN = 500
_CLIENT_ERROR_MIN = 400
_TOO_MANY_REQUESTS = 429
_UNAUTHORISED = 401
_FORBIDDEN = 403
#: A courteous, truthful identifier. Not an impersonation of a browser.
_USER_AGENT = "DHRUVA/0.6 (private research; +https://gdeltproject.org)"
#: Fallback for a caller that builds a feed without configuration in hand (for
#: example, a test). A real poll always threads ``settings.news.
#: min_request_interval_seconds`` through explicitly; see ``workers/news.py``.
_DEFAULT_MIN_INTERVAL_SECONDS = 1.0


class GdeltRequestTiming:
    """Serialize requests below a configured floor, shared across one pass.

    One instance is built per poll pass and handed to every
    :class:`GdeltDocFeed` in it, so the floor is enforced across batches, not
    merely within one -- four feeds sharing one gate behave as one paced
    stream of requests rather than four independent bursts. Mirrors
    ``marketdata.infrastructure.zerodha_history.KiteHistoryTiming``, which
    solves the identical problem for Kite; GDELT gets its own copy rather than
    a shared abstraction because the two providers' bounds come from different
    places (Kite's is documented, GDELT's is a courteous default) and nothing
    else in the codebase needs a third.

    Deliberately does not also own retry-backoff sleeping: :class:`GdeltDocFeed`
    already had an injected ``sleeper`` for that before this class existed, and
    every existing test that exercises it still holds unchanged.
    """

    __slots__ = ("_last_request", "_lock", "_min_interval", "_monotonic", "_sleep")

    def __init__(
        self,
        *,
        min_interval_seconds: float = _DEFAULT_MIN_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind the configured floor and an injectable clock and sleeper."""
        self._min_interval = min_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

    async def before_request(self) -> None:
        """Wait until at least the configured floor has elapsed since the last request."""
        async with self._lock:
            now = self._monotonic()
            if self._last_request is not None:
                delay = self._last_request + self._min_interval - now
                if delay > 0:
                    await self._sleep(delay)
            self._last_request = self._monotonic()


@dataclass(frozen=True, slots=True)
class GdeltQuery:
    """One documented DOC 2.0 article-list query."""

    query: str
    timespan: str = "1d"
    max_records: int = 75
    fresh_within: timedelta | None = None

    def parameters(self) -> dict[str, str]:
        """Return the documented query parameters, and only those."""
        return {
            "query": self.query,
            "mode": "artlist",
            "format": "json",
            "timespan": self.timespan,
            "maxrecords": str(self.max_records),
            "sort": "datedesc",
        }


class GdeltDocFeed:
    """Fetch one DOC 2.0 article list and report exactly what happened."""

    __slots__ = ("_client", "_clock", "_query", "_sleeper", "_timing")

    def __init__(
        self,
        client: httpx2.AsyncClient,
        clock: Clock,
        query: GdeltQuery,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        timing: GdeltRequestTiming | None = None,
    ) -> None:
        """Bind an injected HTTP client, clock, query, retry sleeper and pacing gate.

        ``timing`` defaults to a fresh, unshared gate when omitted -- correct
        for one feed used alone, but a poll pass with several feeds must build
        one :class:`GdeltRequestTiming` and pass the same instance to each of
        them, or the floor is only ever enforced within one feed's own retries.
        """
        self._client = client
        self._clock = clock
        self._query = query
        self._sleeper = sleeper
        self._timing = timing or GdeltRequestTiming()

    async def fetch(self) -> NewsFetchResult:
        """Poll GDELT once, retrying only what is worth retrying."""
        retrieved_at = self._clock.now()
        payload: bytes | None = None
        failure: SourceStatus | None = None

        for attempt in range(_MAX_ATTEMPTS):
            await self._timing.before_request()
            try:
                response = await self._client.get(
                    GDELT_DOC_ENDPOINT,
                    params=self._query.parameters(),
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
            except httpx2.TimeoutException:
                failure = self._unavailable("GDELT request timed out", retrieved_at)
            except httpx2.TransportError:
                failure = self._unavailable("GDELT was unreachable", retrieved_at)
            else:
                decided = GdeltDocFeed._classify(response, retrieved_at)
                if decided is None:
                    payload = response.content[:MAX_PAYLOAD_BYTES]
                    failure = None
                    break
                failure = decided
                if decided.health is not SourceHealth.TEMPORARILY_UNAVAILABLE:
                    break
            if attempt + 1 < _MAX_ATTEMPTS:
                await self._sleeper(_BACKOFF_BASE_SECONDS * (2**attempt))

        if payload is None:
            return NewsFetchResult(
                source=gdelt_source(),
                status=failure or self._unavailable("GDELT poll failed", retrieved_at),
                items=(),
                retrieved_at=retrieved_at,
                content_sha256=_EMPTY_SHA256,
                mapper_revision=_TRANSPORT_REVISION,
            )
        return map_artlist(
            payload,
            retrieved_at=retrieved_at,
            fresh_within=self._query.fresh_within,
        )

    @staticmethod
    def _unavailable(reason: str, observed_at: datetime) -> SourceStatus:
        """Build the retryable-failure status."""
        return SourceStatus(
            health=SourceHealth.TEMPORARILY_UNAVAILABLE,
            reason=reason,
            observed_at=observed_at,
        )

    @staticmethod
    def _classify(response: httpx2.Response, observed_at: datetime) -> SourceStatus | None:
        """Return the failure this response represents, or ``None`` if it is usable."""
        status = response.status_code
        if status == _TOO_MANY_REQUESTS:
            return SourceStatus(
                health=SourceHealth.RATE_LIMITED,
                reason="GDELT asked for a slower request rate",
                observed_at=observed_at,
                http_status=status,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        if status in {_UNAUTHORISED, _FORBIDDEN}:
            return SourceStatus(
                health=SourceHealth.AUTHENTICATION_FAILED,
                reason="GDELT refused an anonymous request",
                observed_at=observed_at,
                http_status=status,
            )
        if status >= _SERVER_ERROR_MIN:
            return SourceStatus(
                health=SourceHealth.TEMPORARILY_UNAVAILABLE,
                reason=f"GDELT returned server error {status}",
                observed_at=observed_at,
                http_status=status,
            )
        if status >= _CLIENT_ERROR_MIN:
            # The documented request was rejected, which means this adapter's
            # understanding of the API is wrong. Retrying an unchanged request
            # would only repeat the mistake.
            return SourceStatus(
                health=SourceHealth.UNSUPPORTED_SCHEMA,
                reason=f"GDELT rejected the documented request with {status}",
                observed_at=observed_at,
                http_status=status,
            )
        return None


def _retry_after(value: str | None) -> timedelta | None:
    """Read a bounded ``Retry-After`` in seconds, ignoring anything else."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return timedelta(seconds=min(seconds, _MAX_RETRY_AFTER_SECONDS))


#: SHA-256 of the empty payload, used when no bytes were ever received.
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_TRANSPORT_REVISION = "gdelt-doc2-transport-v1"
