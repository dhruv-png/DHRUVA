"""The Kite adapter exposes one safe read surface and strict CSV normalization."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from dhruva.contexts.reference.infrastructure.zerodha_instruments import (
    KiteInstrumentMasterAdapter,
    parse_instrument_master,
)
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import (
    DataQualityError,
    RateLimitedError,
    UnsafeConfigurationError,
    UpstreamAuthenticationError,
    UpstreamUnavailableError,
)
from dhruva.shared.time.clock import FrozenClock

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = pytest.mark.unit

MARKET_DATE = date(2026, 8, 2)
FETCHED_AT = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
API_KEY_TEXT = "fixture-api-key-123"
ACCESS_TOKEN_TEXT = "fixture-access-token-456"  # noqa: S105 - sanitized fixture
FIXTURE = Path(__file__).parents[2] / "fixtures" / "zerodha" / "instruments_sanitized.csv"


def _payload() -> bytes:
    return FIXTURE.read_bytes()


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url="https://api.kite.trade",
        transport=httpx2.MockTransport(handler),
        timeout=1,
    )


def _adapter(
    client: httpx2.AsyncClient,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> KiteInstrumentMasterAdapter:
    kwargs: dict[str, object] = {
        "client": client,
        "api_key": SecretValue(API_KEY_TEXT, register=False),
        "access_token": SecretValue(ACCESS_TOKEN_TEXT, register=False),
        "clock": FrozenClock(FETCHED_AT),
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    return KiteInstrumentMasterAdapter(**kwargs)  # type: ignore[arg-type]


def test_sanitized_master_parses_exact_symbols_and_contract_fields() -> None:
    """Protected punctuation and exact derivative decimals survive CSV parsing."""
    entries = parse_instrument_master(_payload())

    cash_symbols = {item.trading_symbol for item in entries if item.exchange == "NSE"}
    adani_future = next(item for item in entries if item.instrument_token == 300001)
    assert len(entries) == 31
    assert "NAM-INDIA" in cash_symbols
    assert "M&M" in cash_symbols
    assert adani_future.segment == "NFO-FUT"
    assert adani_future.instrument_type == "FUT"
    assert adani_future.expiry == date(2026, 8, 27)
    assert str(adani_future.tick_size) == "0.05"
    assert adani_future.lot_size == 125


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"instrument_token,exchange_token\n1,2\n", "missing required columns"),
        (
            b"instrument_token,exchange_token,tradingsymbol,name,expiry,strike,tick_size,"
            b"lot_size,instrument_type,segment,exchange\n"
            b"1,2,ABC,ABC,not-a-date,0,0.05,1,EQ,NSE,NSE\n",
            "malformed row",
        ),
    ],
)
def test_schema_drift_and_malformed_rows_fail_closed(payload: bytes, message: str) -> None:
    """Provider schema or value drift cannot silently produce instrument facts."""
    with pytest.raises(DataQualityError, match=message):
        parse_instrument_master(payload)


_HEADER = (
    b"instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    b"tick_size,lot_size,instrument_type,segment,exchange\n"
)


def _row(  # noqa: PLR0913 - one keyword per CSV column, all defaulted
    *,
    instrument_token: str = "1",  # noqa: S107 - a public provider row id, not a secret
    exchange_token: str = "2",  # noqa: S107 - a public provider row id, not a secret
    tradingsymbol: str = "TESTIDX",
    name: str = "TEST INDEX",
    last_price: str = "0",
    expiry: str = "",
    strike: str = "",
    tick_size: str = "",
    lot_size: str = "",
    instrument_type: str = "EQ",
    segment: str = "INDICES",
    exchange: str = "NSE",
) -> bytes:
    """Build one header plus one synthetic data row, structurally like a live one.

    Defaults model a non-tradeable index row -- blank expiry, strike, tick size
    and lot size, exactly as Zerodha's own forum moderators describe: "INDICES
    can't be traded, so all those irrelevant fields are null." Every value here
    is synthetic; nothing is copied from a real fetch.
    """
    fields = (
        instrument_token,
        exchange_token,
        tradingsymbol,
        name,
        last_price,
        expiry,
        strike,
        tick_size,
        lot_size,
        instrument_type,
        segment,
        exchange,
    )
    return _HEADER + (",".join(fields) + "\n").encode()


def test_a_non_tradeable_row_with_blank_optional_numeric_fields_is_accepted() -> None:
    """A live compatibility regression: an index row must not poison the whole fetch.

    Zerodha's real instrument dump nulls strike, tick size and lot size for
    rows that can never be traded -- this is structurally the shape that
    previously raised DHR-DQL-001 on a live fetch, reproduced synthetically.
    """
    entries = parse_instrument_master(_row(strike="", tick_size="", lot_size=""))

    assert len(entries) == 1
    entry = entries[0]
    assert entry.strike == Decimal("0")
    assert entry.tick_size == Decimal("0")
    assert entry.lot_size == 0
    assert entry.segment == "INDICES"
    assert entry.instrument_type == "EQ"


def test_a_present_but_unparseable_tick_size_is_still_refused_with_a_precise_diagnostic() -> None:
    """Blank is accepted; garbled is not -- and the failure names its own column."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(tick_size="not-a-decimal"))

    context = captured.value.context
    assert context["row_number"] == 2
    assert context["column"] == "tick_size"
    assert context["exchange"] == "NSE"
    assert context["segment"] == "INDICES"
    assert context["instrument_type"] == "EQ"
    assert context["trading_symbol"] == "TESTIDX"


def test_a_negative_tick_size_is_still_refused() -> None:
    """A present, explicit negative value is a domain violation, not an absence."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(tick_size="-1"))

    context = captured.value.context
    assert context["column"] == "tick_size"
    assert "negative" in context["reason"]


def test_a_negative_lot_size_is_still_refused() -> None:
    """The same guarantee holds for lot size, not only for tick size."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(lot_size="-5"))

    assert captured.value.context["column"] == "lot_size"


def test_a_blank_required_identity_field_is_still_refused() -> None:
    """Exchange, segment and instrument_type stay required: blank there is unresolvable."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(segment=""))

    context = captured.value.context
    assert context["column"] == "segment"
    assert context["row_number"] == 2


def test_a_blank_trading_symbol_is_still_refused() -> None:
    """The instrument's own identifier is never optional."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(tradingsymbol=""))

    assert captured.value.context["column"] == "tradingsymbol"


def test_the_expiry_format_diagnostic_names_its_own_column() -> None:
    """The pre-existing malformed-expiry refusal now also names the column."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(expiry="not-a-date"))

    assert captured.value.context["column"] == "expiry"


def test_a_bse_equity_name_with_surrounding_whitespace_is_normalized_not_refused() -> None:
    """A live compatibility regression, reproduced structurally with a synthetic value.

    A live fetch was refused on exactly this shape -- exchange BSE, segment
    BSE, instrument_type EQ, tradingsymbol BIRLACABLE -- because the
    registrar-sourced ``name`` carried incidental surrounding whitespace. The
    company name below is a placeholder, not the real one; only the
    structural shape is reproduced.
    """
    entries = parse_instrument_master(
        _row(
            tradingsymbol="BIRLACABLE",
            name="  Sample Cable Industries Limited  ",
            instrument_type="EQ",
            segment="BSE",
            exchange="BSE",
        )
    )

    assert len(entries) == 1
    assert entries[0].name == "Sample Cable Industries Limited"
    assert entries[0].trading_symbol == "BIRLACABLE"


def test_internal_whitespace_in_a_name_is_preserved_not_collapsed() -> None:
    """Only the edges are trimmed -- this is boundary normalization, not reformatting."""
    entries = parse_instrument_master(_row(name="  Sample  Cable  Industries  "))

    assert entries[0].name == "Sample  Cable  Industries"


def test_a_name_still_invalid_after_stripping_whitespace_is_refused() -> None:
    """Stripping removes formatting noise; it does not widen what a name may be."""
    too_long = "A" * 201
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(name=f"  {too_long}  "))

    context = captured.value.context
    assert context["column"] == "name"
    assert "too long" in context["reason"]


def test_a_blank_name_and_expiry_remain_accepted() -> None:
    """Unchanged behavior: a derivative has no name; an equity has no expiry."""
    entries = parse_instrument_master(
        _row(name="", expiry="", instrument_type="FUT", segment="NFO-FUT", exchange="NFO")
    )

    assert entries[0].name == ""
    assert entries[0].expiry is None


def test_the_diagnostic_never_leaks_more_than_the_documented_public_columns() -> None:
    """Only structural, public instrument facts appear in the error -- nothing else."""
    with pytest.raises(DataQualityError) as captured:
        parse_instrument_master(_row(tick_size="garbage"))

    assert set(captured.value.context) == {
        "row_number",
        "column",
        "reason",
        "exchange",
        "segment",
        "instrument_type",
        "trading_symbol",
    }


def test_duplicate_provider_tokens_are_rejected() -> None:
    """One snapshot cannot identify two rows with the same current token."""
    lines = _payload().splitlines()
    duplicate = b"\n".join((*lines, lines[1], b""))

    with pytest.raises(DataQualityError, match="duplicate instrument tokens"):
        parse_instrument_master(duplicate)


@pytest.mark.asyncio
async def test_adapter_rejects_nonofficial_or_redirecting_clients() -> None:
    """Credentials cannot be sent over plaintext or followed onto another host."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=_payload(), request=request)

    async with httpx2.AsyncClient(
        base_url="http://api.kite.trade",
        transport=httpx2.MockTransport(handler),
    ) as insecure:
        with pytest.raises(UnsafeConfigurationError, match="official TLS"):
            _adapter(insecure)

    async with httpx2.AsyncClient(
        base_url="https://api.kite.trade",
        transport=httpx2.MockTransport(handler),
        follow_redirects=True,
    ) as redirecting:
        with pytest.raises(UnsafeConfigurationError, match="redirects"):
            _adapter(redirecting)


@pytest.mark.asyncio
async def test_adapter_uses_only_documented_read_headers_and_hashes_the_dump() -> None:
    """The adapter signs one GET without exposing SDK types downstream."""
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["version"] = request.headers["X-Kite-Version"]
        seen["authorization"] = request.headers["Authorization"]
        return httpx2.Response(200, content=_payload(), request=request)

    async with _client(handler) as client:
        snapshot = await _adapter(client).fetch(market_date=MARKET_DATE)

    assert seen == {
        "method": "GET",
        "path": "/instruments",
        "version": "3",
        "authorization": f"token {API_KEY_TEXT}:{ACCESS_TOKEN_TEXT}",
    }
    assert snapshot.market_date == MARKET_DATE
    assert snapshot.fetched_at == FETCHED_AT
    assert snapshot.content_sha256 == hashlib.sha256(_payload()).hexdigest()
    assert snapshot.raw_csv == _payload()


@pytest.mark.asyncio
async def test_expired_session_fails_without_leaking_credentials() -> None:
    """A rejected token becomes an operator-actionable error, never a retry loop."""
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(401, request=request)

    async with _client(handler) as client:
        with pytest.raises(UpstreamAuthenticationError) as captured:
            await _adapter(client).fetch(market_date=MARKET_DATE)

    rendered = str(captured.value)
    assert calls == 1
    assert API_KEY_TEXT not in rendered
    assert ACCESS_TOKEN_TEXT not in rendered


@pytest.mark.asyncio
async def test_rate_limit_retries_with_bounded_retry_after() -> None:
    """A temporary throttle backs off and succeeds without changing the request."""
    calls = 0
    delays: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx2.Response(
                429,
                headers={"Retry-After": "999"},
                request=request,
            )
        return httpx2.Response(200, content=_payload(), request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with _client(handler) as client:
        snapshot = await _adapter(client, sleep=record_sleep).fetch(market_date=MARKET_DATE)

    assert len(snapshot.entries) == 31
    assert calls == 3
    assert delays == [30.0, 30.0]


@pytest.mark.asyncio
async def test_persistent_rate_limit_and_server_errors_surface_distinctly() -> None:
    """Exhausted retries preserve whether the provider throttled or failed."""

    async def no_sleep(delay: float) -> None:  # noqa: ARG001 - injected retry sink
        return None

    def throttle(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, request=request)

    async with _client(throttle) as client:
        with pytest.raises(RateLimitedError):
            await _adapter(client, sleep=no_sleep).fetch(market_date=MARKET_DATE)

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, request=request)

    async with _client(unavailable) as client:
        with pytest.raises(UpstreamUnavailableError):
            await _adapter(client, sleep=no_sleep).fetch(market_date=MARKET_DATE)
