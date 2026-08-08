"""The Kite Connect login handshake, and nothing else (ADR-076).

Two operations, verified against the official Kite Connect v3 documentation on
2026-08-08:

1. The login page is ``https://kite.zerodha.com/connect/login?v=3&api_key=…``.
   A successful login returns a ``request_token`` as a query parameter on the
   redirect URL registered for that key.
2. ``POST https://api.kite.trade/session/token`` with ``api_key``,
   ``request_token`` and ``checksum`` -- the SHA-256 of
   ``api_key + request_token + api_secret`` -- returns the session, including
   ``access_token``, ``user_id`` and ``login_time``. The token "will expire at
   6 AM on the next day (regulatory requirement)".

That last sentence is why expiry is computed rather than guessed, and why this
adapter is the thing that computes it: the rule is the broker's, so it belongs
beside the broker's other specifics.

**No automation of the login itself.** This constructs a URL for the operator to
open in their own browser and accepts the token that comes back. There is no
username, no password, no TOTP and no scraping anywhere in this module, and
there must not be: Zerodha's login UI is the control that makes a stolen API
secret insufficient on its own.

The response carries fifteen fields. Three are read. The rest -- email, avatar
URL, enabled exchanges, permitted order types, the ``enctoken`` -- are not
stored, not logged and not parsed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode

import httpx2

from dhruva.contexts.platform.domain.broker.ports import BrokerSessionMaterial
from dhruva.contexts.platform.domain.broker.session import next_expiry_after
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import (
    DataQualityError,
    RateLimitedError,
    UpstreamAuthenticationError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

if TYPE_CHECKING:
    from dhruva.contexts.platform.domain.broker.session import BrokerApplication
    from dhruva.shared.time.clock import Clock

__all__ = ["KiteAuthenticator", "parse_session_response"]

KITE_LOGIN_URL: Final = "https://kite.zerodha.com/connect/login"
KITE_API_BASE: Final = "https://api.kite.trade"
_SESSION_ENDPOINT: Final = "/session/token"
_KITE_VERSION: Final = "3"
_MAX_RESPONSE_BYTES: Final = 256 * 1024
_HTTP_OK: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_HTTP_FORBIDDEN: Final = 403
_HTTP_TOO_MANY_REQUESTS: Final = 429
_HTTP_SERVER_ERROR_MIN: Final = 500

#: Kite returns ``login_time`` in the broker's local time with no offset.
_LOGIN_TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"


class KiteAuthenticator:
    """Builds the login URL and performs the request-token exchange.

    Holds no credential of its own. The application secret arrives as an
    argument for the length of one call and is used only to compute a checksum;
    keeping it out of the constructor means an instance of this class is not
    something worth stealing.
    """

    __slots__ = ("_base_url", "_client", "_clock", "_login_url")

    def __init__(
        self,
        client: httpx2.AsyncClient,
        clock: Clock,
        *,
        base_url: str = KITE_API_BASE,
        login_url: str = KITE_LOGIN_URL,
    ) -> None:
        """Bind the adapter to an HTTP client and an injected clock (ADR-011)."""
        self._client = client
        self._clock = clock
        self._base_url = base_url.rstrip("/")
        self._login_url = login_url

    def login_url(self, application: BrokerApplication) -> str:
        """Return the official Kite login page for this application.

        ``v=3`` is part of the documented URL rather than decoration: it selects
        the version of the flow whose redirect carries a ``request_token``.
        """
        query = urlencode({"v": _KITE_VERSION, "api_key": application.identifier.reveal()})
        return f"{self._login_url}?{query}"

    async def exchange(
        self,
        application: BrokerApplication,
        request_token: SecretValue,
    ) -> BrokerSessionMaterial:
        """Exchange the request token for session material.

        Raises
        ------
        UpstreamAuthenticationError
            The broker rejected the token or the checksum. Both arrive as 400 or
            403 and are reported as one thing deliberately: from here they are
            indistinguishable, and inventing a distinction would tell the
            operator something this code does not know.
        UpstreamTimeoutError, UpstreamUnavailableError, RateLimitedError
            Transport conditions to retry rather than fix.
        DataQualityError
            The answer was accepted but could not be understood.
        """
        api_key = application.identifier.reveal()
        checksum = hashlib.sha256(
            f"{api_key}{request_token.reveal()}{application.secret.reveal()}".encode()
        ).hexdigest()

        try:
            response = await self._client.post(
                f"{self._base_url}{_SESSION_ENDPOINT}",
                headers={"X-Kite-Version": _KITE_VERSION},
                data={
                    "api_key": api_key,
                    "request_token": request_token.reveal(),
                    "checksum": checksum,
                },
            )
        except httpx2.TimeoutException as error:
            message = "the broker did not answer the session exchange in time"
            raise UpstreamTimeoutError(message, provider="zerodha") from error
        except httpx2.HTTPError as error:
            # The exception is not interpolated into the message: an httpx error
            # repeats the request URL, and this request's body carries a secret.
            message = "the session exchange could not reach the broker"
            raise UpstreamUnavailableError(message, provider="zerodha") from error

        _raise_for_status(response.status_code)
        return parse_session_response(response.content[:_MAX_RESPONSE_BYTES])


def _raise_for_status(status_code: int) -> None:
    """Map a status code onto the error an operator can act on."""
    if status_code == _HTTP_OK:
        return
    if status_code in (_HTTP_BAD_REQUEST, _HTTP_FORBIDDEN):
        message = (
            "the broker rejected the login: the request token may have already "
            "been used or expired, or the enrolled application secret may be wrong"
        )
        raise UpstreamAuthenticationError(message, provider="zerodha", status=status_code)
    if status_code == _HTTP_TOO_MANY_REQUESTS:
        message = "the broker rate-limited the session exchange"
        raise RateLimitedError(message, provider="zerodha", status=status_code)
    if status_code >= _HTTP_SERVER_ERROR_MIN:
        message = "the broker returned a server error for the session exchange"
        raise UpstreamUnavailableError(message, provider="zerodha", status=status_code)
    message = "the broker returned an unexpected status for the session exchange"
    raise UpstreamUnavailableError(message, provider="zerodha", status=status_code)


def parse_session_response(body: bytes) -> BrokerSessionMaterial:
    """Read the three fields this product needs out of a Kite session response.

    Separate from the adapter so it can be tested against recorded payloads with
    no HTTP at all, and so the "what we ignore" decision is readable in one
    place.

    Nothing from the body reaches an exception message. A malformed response is
    reported by shape -- which key was missing -- because the body being parsed
    contains an access token whenever it is nearly-valid, and a parser that
    quotes its input is a parser that logs secrets on the unhappy path.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        message = "the broker's session response was not JSON"
        raise DataQualityError(message, provider="zerodha") from None
    if not isinstance(payload, dict):
        message = "the broker's session response was not an object"
        raise DataQualityError(message, provider="zerodha")

    data = payload.get("data")
    if not isinstance(data, dict):
        message = "the broker's session response carried no data object"
        raise DataQualityError(message, provider="zerodha", status=repr(payload.get("status")))

    access_token = _required(data, "access_token")
    user_id = _required(data, "user_id")
    issued_at = _login_time(data)
    return BrokerSessionMaterial(
        token=SecretValue(access_token, register=True),
        broker_user_id=user_id,
        issued_at=issued_at,
        expires_at=next_expiry_after(issued_at),
    )


def _required(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        message = f"the broker's session response is missing {field}"
        raise DataQualityError(message, provider="zerodha", field=field)
    return value


def _login_time(data: dict[str, Any]) -> datetime:
    """Parse ``login_time``, which Kite sends as IST with no offset.

    Refused rather than defaulted when absent or unreadable. A missing login
    time would otherwise be replaced by "now", and "now" on a machine whose
    clock is wrong produces an expiry that is wrong in the dangerous direction:
    a session believed live after the broker has already killed it.
    """
    raw = _required(data, "login_time")
    try:
        naive = datetime.strptime(raw, _LOGIN_TIME_FORMAT)  # noqa: DTZ007 - offset applied below
    except ValueError:
        message = "the broker's session response holds an unreadable login_time"
        raise DataQualityError(message, provider="zerodha", field="login_time") from None
    return (naive - timedelta(hours=5, minutes=30)).replace(tzinfo=UTC)
