"""The Kite login handshake, against a fake transport (ADR-076).

No network. The client is an ``httpx2.AsyncClient`` over a
``MockTransport``, which is the real client doing real request construction
against a handler that answers in-process -- so what is asserted is the request
DHRUVA would actually have sent, not a description of it.

Three things matter here beyond the happy path. The checksum must be exactly
what Kite documents, because a wrong one is indistinguishable at the operator's
end from a wrong secret. Every failure mode must map onto an error the operator
can act on. And nothing -- no message, no context, no log line -- may repeat the
secret, the request token or the access token, all three of which pass through
this module within a few lines of each other.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx2
import pytest

from dhruva.contexts.platform.domain.broker.session import BrokerApplication
from dhruva.contexts.platform.infrastructure.zerodha.auth import (
    KiteAuthenticator,
    parse_session_response,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import (
    DataQualityError,
    RateLimitedError,
    UpstreamAuthenticationError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from dhruva.shared.time.clock import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit

#: Obviously synthetic throughout. None of these is or resembles real material.
IDENTIFIER: Final = "synthetic-api-identifier"
APPLICATION_MATERIAL: Final = "synthetic-application-material"
REQUEST_MATERIAL: Final = "synthetic-request-material"
SESSION_MATERIAL: Final = "synthetic-session-material"

NOW: Final = datetime(2026, 8, 8, 4, 30, tzinfo=UTC)


def _application() -> BrokerApplication:
    return BrokerApplication(
        identifier=SecretValue(IDENTIFIER, register=False),
        secret=SecretValue(APPLICATION_MATERIAL, register=False),
    )


def _session_body(**overrides: object) -> bytes:
    data: dict[str, object] = {
        "access_token": SESSION_MATERIAL,
        "user_id": "XX0000",
        "login_time": "2026-08-08 10:00:00",
        # Fields Kite returns and this product deliberately ignores. Present so
        # the parser is exercised against a realistic payload rather than a
        # trimmed one -- a parser tested only on the fields it wants will not
        # notice the day it starts storing one it does not.
        "email": "synthetic@example.invalid",
        "exchanges": ["NSE", "NFO"],
        "order_types": ["MARKET", "LIMIT"],
        "enctoken": "synthetic-enctoken",
        "public_token": "synthetic-public-token",
    }
    data.update(overrides)
    return json.dumps({"status": "success", "data": data}).encode()


def _authenticator(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[KiteAuthenticator, httpx2.AsyncClient]:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return KiteAuthenticator(client, FrozenClock(NOW)), client


# --------------------------------------------------------------------------- #
# The login URL
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_login_url_is_the_official_endpoint_with_the_documented_version() -> None:
    """``v=3`` selects the flow whose redirect carries a request token.

    Omitting it silently gets an older handshake, and the operator's first sign
    that anything is wrong is a redirect with no token on it.
    """
    authenticator, client = _authenticator(lambda _request: httpx2.Response(200))
    async with client:
        url = authenticator.login_url(_application())

    assert url.startswith("https://kite.zerodha.com/connect/login?")
    assert "v=3" in url
    assert f"api_key={IDENTIFIER}" in url


@pytest.mark.asyncio
async def test_the_login_url_carries_no_secret() -> None:
    """Only the public half. The URL is printed to a terminal and often pasted.

    The api_key identifies the application and is meant to travel in a URL; the
    api_secret never leaves this process except as a hash.
    """
    authenticator, client = _authenticator(lambda _request: httpx2.Response(200))
    async with client:
        url = authenticator.login_url(_application())

    assert APPLICATION_MATERIAL not in url


# --------------------------------------------------------------------------- #
# The exchange
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_exchange_sends_exactly_what_kite_documents() -> None:
    """api_key, request_token and the SHA-256 checksum, with the version header.

    The checksum is recomputed here from the documented formula rather than
    copied from the implementation. A test that asked the code what the hash
    should be would pass for any hash.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["version"] = request.headers.get("X-Kite-Version", "")
        seen["body"] = request.content.decode()
        return httpx2.Response(200, content=_session_body())

    authenticator, client = _authenticator(handler)
    async with client:
        await authenticator.exchange(_application(), SecretValue(REQUEST_MATERIAL, register=False))

    expected = hashlib.sha256(
        f"{IDENTIFIER}{REQUEST_MATERIAL}{APPLICATION_MATERIAL}".encode()
    ).hexdigest()
    assert seen["url"] == "https://api.kite.trade/session/token"
    assert seen["version"] == "3"
    assert f"api_key={IDENTIFIER}" in seen["body"]
    assert f"request_token={REQUEST_MATERIAL}" in seen["body"]
    assert f"checksum={expected}" in seen["body"]


@pytest.mark.asyncio
async def test_the_application_secret_is_never_sent_only_its_hash() -> None:
    """The whole point of the checksum construction, asserted on the wire."""
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = request.content.decode()
        seen["url"] = str(request.url)
        return httpx2.Response(200, content=_session_body())

    authenticator, client = _authenticator(handler)
    async with client:
        await authenticator.exchange(_application(), SecretValue(REQUEST_MATERIAL, register=False))

    assert APPLICATION_MATERIAL not in seen["body"]
    assert APPLICATION_MATERIAL not in seen["url"]


@pytest.mark.asyncio
async def test_a_successful_exchange_returns_the_three_facts_that_are_stored() -> None:
    """Token, broker user id, and an expiry derived from the broker's login time."""
    authenticator, client = _authenticator(
        lambda _request: httpx2.Response(200, content=_session_body())
    )
    async with client:
        material = await authenticator.exchange(
            _application(), SecretValue(REQUEST_MATERIAL, register=False)
        )

    assert material.token.reveal() == SESSION_MATERIAL
    assert material.broker_user_id == "XX0000"
    # 10:00 IST on the 8th -> 04:30 UTC; expiry 06:00 IST on the 9th -> 00:30 UTC.
    assert material.issued_at == datetime(2026, 8, 8, 4, 30, tzinfo=UTC)
    assert material.expires_at == datetime(2026, 8, 9, 0, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_expiry_comes_from_the_broker_not_from_this_machine() -> None:
    """A wrong local clock must not extend a session past the broker's cut-off.

    The clock here is set two days after the login time. If expiry were computed
    from "now", the stored session would outlive the broker's by 48 hours and
    every subsequent request would fail with an authentication error that looked
    like a credential problem.
    """
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, content=_session_body())
        )
    )
    authenticator = KiteAuthenticator(client, FrozenClock(datetime(2026, 8, 10, tzinfo=UTC)))
    async with client:
        material = await authenticator.exchange(
            _application(), SecretValue(REQUEST_MATERIAL, register=False)
        )

    assert material.expires_at == datetime(2026, 8, 9, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("status", [400, 403], ids=["bad-request", "forbidden"])
@pytest.mark.asyncio
async def test_a_rejected_login_is_an_authentication_error(status: int) -> None:
    """A used, expired or mistyped request token, or a wrong secret.

    Reported as one thing on purpose: the broker does not distinguish them in a
    way this code can read, and inventing a distinction would send the operator
    to rotate a secret that was fine.
    """
    authenticator, client = _authenticator(lambda _request: httpx2.Response(status))
    async with client:
        with pytest.raises(UpstreamAuthenticationError) as caught:
            await authenticator.exchange(
                _application(), SecretValue(REQUEST_MATERIAL, register=False)
            )

    assert "request token" in str(caught.value)


@pytest.mark.asyncio
async def test_rate_limiting_is_distinct_from_rejection() -> None:
    """The operator waits rather than re-enrols."""
    authenticator, client = _authenticator(lambda _request: httpx2.Response(429))
    async with client:
        with pytest.raises(RateLimitedError):
            await authenticator.exchange(
                _application(), SecretValue(REQUEST_MATERIAL, register=False)
            )


@pytest.mark.parametrize("status", [500, 503], ids=["server-error", "unavailable"])
@pytest.mark.asyncio
async def test_a_broker_outage_is_not_reported_as_a_credential_problem(status: int) -> None:
    """Otherwise the operator's first move is to rotate a working secret."""
    authenticator, client = _authenticator(lambda _request: httpx2.Response(status))
    async with client:
        with pytest.raises(UpstreamUnavailableError):
            await authenticator.exchange(
                _application(), SecretValue(REQUEST_MATERIAL, register=False)
            )


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_a_timeout() -> None:
    """Retryable, and distinguishable from a refusal in a log."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("timed out", request=request)

    authenticator, client = _authenticator(handler)
    async with client:
        with pytest.raises(UpstreamTimeoutError):
            await authenticator.exchange(
                _application(), SecretValue(REQUEST_MATERIAL, register=False)
            )


@pytest.mark.asyncio
async def test_a_transport_failure_does_not_repeat_the_request_it_failed_on() -> None:
    """An httpx error names the URL and can carry the request; the body is secret.

    This asserts the outcome rather than the mechanism, because "do not
    interpolate the exception" is a rule a future edit can break without
    noticing.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    authenticator, client = _authenticator(handler)
    async with client:
        with pytest.raises(UpstreamUnavailableError) as caught:
            await authenticator.exchange(
                _application(), SecretValue(REQUEST_MATERIAL, register=False)
            )

    rendered = f"{caught.value}{caught.value.context}"
    assert APPLICATION_MATERIAL not in rendered
    assert REQUEST_MATERIAL not in rendered


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_the_parser_ignores_everything_it_was_not_asked_for() -> None:
    """Fifteen fields arrive; three are read.

    Asserted by name because "we do not store the email" is a privacy claim, and
    a claim nobody checks is one that stops being true when somebody adds a
    field "while they are in there".
    """
    material = parse_session_response(_session_body())

    rendered = repr(material)
    for ignored in ("synthetic@example.invalid", "NSE", "MARKET", "synthetic-enctoken"):
        assert ignored not in rendered


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"status": "error"}',
        b'{"status": "success", "data": {"user_id": "X0", "login_time": "2026-08-08 10:00:00"}}',
        b'{"status": "success", "data": {"access_token": "t", "login_time": "2026-08-08 10:00"}}',
    ],
    ids=["not-json", "not-an-object", "no-data", "no-token", "no-user-id"],
)
def test_a_malformed_response_is_refused(body: bytes) -> None:
    """Guessing here would store a session that cannot work and looks like one."""
    with pytest.raises(DataQualityError):
        parse_session_response(body)


@pytest.mark.parametrize(
    "login_time",
    ["", "yesterday", "2026-08-08T10:00:00+05:30", "08/08/2026 10:00"],
    ids=["empty", "prose", "iso-with-offset", "other-format"],
)
def test_an_unreadable_login_time_is_refused_rather_than_defaulted(login_time: str) -> None:
    """Defaulting to "now" produces an expiry that is wrong in the worst direction.

    A session whose expiry is later than the broker's is one the system believes
    in after it has stopped working -- so every downstream call fails and the
    status command cheerfully reports SUCCESS.
    """
    with pytest.raises(DataQualityError):
        parse_session_response(_session_body(login_time=login_time))


def test_a_parse_failure_never_quotes_the_body() -> None:
    """The body carries an access token whenever it is nearly-valid."""
    body = _session_body(user_id="")

    with pytest.raises(DataQualityError) as caught:
        parse_session_response(body)

    rendered = f"{caught.value}{caught.value.context}"
    assert SESSION_MATERIAL not in rendered


def test_the_adapter_imports_nothing_that_could_place_an_order() -> None:
    """The credential-holding module must not be one edit away from trading.

    Parsed rather than grepped so a mention in prose does not fail the build.
    """
    import ast  # noqa: PLC0415 - test-only import
    import pathlib  # noqa: PLC0415 - test-only import

    import dhruva.contexts.platform.infrastructure.zerodha.auth as module  # noqa: PLC0415

    tree = ast.parse(pathlib.Path(module.__file__ or "").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    joined = " ".join(imported).lower()
    for forbidden in ("order", "trading", "gtt", "portfolio", "position", "holding"):
        assert forbidden not in joined
