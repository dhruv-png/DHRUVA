"""The GDELT transport reports every operational outcome as itself."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from dhruva.contexts.intelligence.domain.sources import SourceHealth
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import (
    GDELT_DOC_ENDPOINT,
    GdeltDocFeed,
    GdeltQuery,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

FIXTURES = Path(__file__).parents[2] / "fixtures" / "gdelt"
RETRIEVED = datetime(2026, 8, 3, 7, 0, tzinfo=UTC)
QUERY = GdeltQuery(query='("HAL" OR "State Bank of India") sourcecountry:india')


class FrozenClock:
    """Return one fixed instant, because time is injected (ADR-011)."""

    def now(self) -> datetime:
        """Return the arranged retrieval instant."""
        return RETRIEVED


class FakeTransport:
    """Answer each GET with an arranged response or exception, and record calls."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, dict[str, str], dict[str, str]]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> httpx2.Response:
        """Return the next arranged answer, raising it when it is an exception."""
        self.requests.append((url, params, headers))
        answer = self.answers.pop(0) if self.answers else self.answers
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, httpx2.Response)
        return answer


class NoSleep:
    """Record backoff delays without spending them."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record one requested delay."""
        self.delays.append(seconds)


def _response(status: int, content: bytes = b"", **headers: str) -> httpx2.Response:
    return httpx2.Response(status_code=status, content=content, headers=headers)


def _feed(transport: Any, sleeper: Any, query: GdeltQuery = QUERY) -> GdeltDocFeed:
    return GdeltDocFeed(transport, FrozenClock(), query, sleeper)


def _sanitized() -> bytes:
    return (FIXTURES / "artlist_sanitized.json").read_bytes()


# --------------------------------------------------------------------------- #
# The request itself
# --------------------------------------------------------------------------- #


async def test_only_the_documented_endpoint_and_parameters_are_requested() -> None:
    """No key, no session, no undocumented parameter."""
    transport = FakeTransport(_response(200, _sanitized()))

    await _feed(transport, NoSleep()).fetch()

    url, params, headers = transport.requests[0]
    assert url == GDELT_DOC_ENDPOINT
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["sort"] == "datedesc"
    assert set(params) == {"query", "mode", "format", "timespan", "maxrecords", "sort"}
    assert "Cookie" not in headers
    assert "Authorization" not in headers


async def test_the_user_agent_identifies_dhruva_rather_than_imitating_a_browser() -> None:
    """A truthful identifier is the difference between polling and pretending."""
    transport = FakeTransport(_response(200, _sanitized()))

    await _feed(transport, NoSleep()).fetch()

    agent = transport.requests[0][2]["User-Agent"]
    assert agent.startswith("DHRUVA/")
    assert "Mozilla" not in agent
    assert "Chrome" not in agent


async def test_the_article_url_is_never_fetched() -> None:
    """DHRUVA stores the publisher's link; it does not crawl it."""
    transport = FakeTransport(_response(200, _sanitized()))

    result = await _feed(transport, NoSleep()).fetch()

    assert len(transport.requests) == 1
    assert result.items
    assert all(request[0] == GDELT_DOC_ENDPOINT for request in transport.requests)


# --------------------------------------------------------------------------- #
# Health outcomes
# --------------------------------------------------------------------------- #


async def test_a_documented_response_is_handed_to_the_mapper() -> None:
    """The transport decides operational outcomes and parses nothing itself."""
    result = await _feed(FakeTransport(_response(200, _sanitized())), NoSleep()).fetch()

    assert result.status.health is SourceHealth.HEALTHY
    assert len(result.items) == 4
    assert result.retrieved_at == RETRIEVED


async def test_rate_limiting_is_its_own_outcome_and_is_not_retried() -> None:
    """Being asked to slow down means wait, not try harder."""
    transport = FakeTransport(_response(429, b"", **{"Retry-After": "30"}))
    sleeper = NoSleep()

    result = await _feed(transport, sleeper).fetch()

    assert result.status.health is SourceHealth.RATE_LIMITED
    assert result.status.http_status == 429
    assert result.status.retry_after == timedelta(seconds=30)
    assert len(transport.requests) == 1
    assert sleeper.delays == []


async def test_an_absurd_retry_after_is_bounded() -> None:
    """A header is a request, not an instruction to sleep for a day."""
    transport = FakeTransport(_response(429, b"", **{"Retry-After": "86400"}))

    result = await _feed(transport, NoSleep()).fetch()

    assert result.status.retry_after == timedelta(seconds=60)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_anonymous_request_is_an_authentication_failure(status: int) -> None:
    """An anonymous source that starts refusing must not read as an outage."""
    result = await _feed(FakeTransport(_response(status)), NoSleep()).fetch()

    assert result.status.health is SourceHealth.AUTHENTICATION_FAILED
    assert result.status.http_status == status


async def test_a_server_error_is_retried_and_then_reported_unavailable() -> None:
    """Three bounded attempts, then an honest answer."""
    transport = FakeTransport(_response(503), _response(503), _response(503))
    sleeper = NoSleep()

    result = await _feed(transport, sleeper).fetch()

    assert result.status.health is SourceHealth.TEMPORARILY_UNAVAILABLE
    assert len(transport.requests) == 3
    assert sleeper.delays == [1.0, 2.0]


async def test_a_transient_failure_that_recovers_returns_the_recovered_poll() -> None:
    """Retrying is only worth doing if a later attempt is actually used."""
    transport = FakeTransport(
        httpx2.TimeoutException("slow"),
        _response(200, _sanitized()),
    )

    result = await _feed(transport, NoSleep()).fetch()

    assert result.status.health is SourceHealth.HEALTHY
    assert len(transport.requests) == 2


async def test_a_timeout_that_never_recovers_is_temporarily_unavailable() -> None:
    """A silent feed is an operational fact, never an empty successful poll."""
    transport = FakeTransport(*(httpx2.TimeoutException("slow") for _ in range(3)))

    result = await _feed(transport, NoSleep()).fetch()

    assert result.status.health is SourceHealth.TEMPORARILY_UNAVAILABLE
    assert result.items == ()
    assert "timed out" in result.status.reason


async def test_a_transport_error_that_never_recovers_is_temporarily_unavailable() -> None:
    """Unreachable is the same class of fact as slow, and is retried the same way."""
    transport = FakeTransport(*(httpx2.ConnectError("no route") for _ in range(3)))

    result = await _feed(transport, NoSleep()).fetch()

    assert result.status.health is SourceHealth.TEMPORARILY_UNAVAILABLE
    assert "unreachable" in result.status.reason


async def test_a_rejected_documented_request_is_an_unsupported_schema() -> None:
    """A 4xx on a documented call means this adapter's understanding is wrong."""
    transport = FakeTransport(_response(400))

    result = await _feed(transport, NoSleep()).fetch()

    assert result.status.health is SourceHealth.UNSUPPORTED_SCHEMA
    assert result.status.http_status == 400
    assert len(transport.requests) == 1


async def test_an_empty_but_successful_poll_stays_distinct_from_an_outage() -> None:
    """Preserving this distinction is the reason the health enum exists at all."""
    empty = (FIXTURES / "artlist_empty.json").read_bytes()

    result = await _feed(FakeTransport(_response(200, empty)), NoSleep()).fetch()

    assert result.status.health is SourceHealth.EMPTY_RESULT
    assert result.status.succeeded


async def test_a_failed_poll_carries_no_items_and_still_names_its_revision() -> None:
    """Even a total failure is a recorded observation with provenance."""
    result = await _feed(
        FakeTransport(_response(503), _response(503), _response(503)), NoSleep()
    ).fetch()

    assert result.items == ()
    assert result.usable_items == ()
    assert result.mapper_revision
    assert result.content_sha256
