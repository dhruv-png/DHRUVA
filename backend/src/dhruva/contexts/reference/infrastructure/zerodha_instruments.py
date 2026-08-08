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
    except _MalformedRowError as error:
        raise DataQualityError(
            "instrument master contains a malformed row",
            row_number=row_number,
            column=error.field,
            reason=error.reason,
            exchange=error.exchange,
            segment=error.segment,
            instrument_type=error.instrument_type,
            trading_symbol=error.trading_symbol,
        ) from None
    except (InvalidOperation, KeyError, TypeError, ValueError, InvariantViolation):
        # Defensive net: nothing observed should still reach this branch now that
        # every field in _entry is parsed under its own name, but a row that
        # fails before any field is even read (for example an unparseable CSV
        # dialect quirk) must still fail closed rather than propagate raw.
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


class _MalformedRowError(Exception):
    """One CSV row failed to parse, with enough safe context to diagnose why.

    Carries no secret and no more of the row than the columns a diagnostic
    needs: which column failed, a short classification of why, and the
    instrument's own public identity (exchange, segment, instrument_type,
    trading symbol) -- read directly from the raw row rather than from
    whatever this entry's own parsing had already completed, so the context
    is present regardless of which field failed first.
    """

    def __init__(
        self,
        *,
        field: str,
        reason: str,
        row: dict[str, str | None],
    ) -> None:
        """Capture the failing column and the row's own safe public identity."""
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason
        self.exchange = row.get("exchange") or "?"
        self.segment = row.get("segment") or "?"
        self.instrument_type = row.get("instrument_type") or "?"
        self.trading_symbol = row.get("tradingsymbol") or "?"


#: Non-tradeable rows (an index cannot be bought or sold) carry no meaningful
#: tick size or lot size, and the official schema does not document every
#: exchange/segment combination that leaves them blank -- a Zerodha moderator
#: confirms indices specifically null out "irrelevant" numeric fields rather
#: than sending a zero. Treated exactly like ``strike``, which already allows
#: blank for the same reason (an equity has no strike price). Blank becomes
#: the domain's own zero rather than a parse failure; a present-but-garbled
#: value still fails closed, and a present-but-negative one is still refused
#: by ``InstrumentMasterEntry.__post_init__``. Nothing downstream is weakened
#: by this: futures eligibility already requires ``lot_size > 0 and
#: tick_size > 0`` on its own (``instrument_discovery._futures_candidates``),
#: so a zero-lot row simply becomes an ordinary, correctly non-eligible entry
#: instead of a row that poisons the entire daily fetch.
_BLANK_AS_ZERO = frozenset({"strike", "tick_size", "lot_size"})

#: Columns read as free-form text with no numeric meaning; blank is a
#: legitimate absence (a derivative has no ``name``; an equity has no
#: ``expiry``), not a placeholder for anything.
_BLANK_ALLOWED = frozenset({"name", "expiry"})


def _entry(row: dict[str, str | None]) -> InstrumentMasterEntry:
    """Convert one CSV row using exact decimal and date parsing.

    Every field is read under its own name so a failure -- blank, unparseable,
    or out of range -- can be reported as *that* column rather than as an
    opaque row-level failure.

    ``name`` is stripped here, and only here. It is free text carried through
    from each exchange's own registrar feed rather than a value Kite
    generates, and BSE's in particular is observed to include incidental
    leading/trailing whitespace on otherwise-valid rows (a live fetch refused
    on exactly this for ``BIRLACABLE`` on BSE). Stripping removes nothing a
    reader would consider part of the company's name; it is normalization of
    provider formatting noise, not a relaxation of what a name may contain.
    Every other column is read exactly as the provider sent it -- a
    ``tradingsymbol``, ``instrument_type``, ``segment`` or ``exchange`` is a
    short controlled-vocabulary code Kite itself produces, not free text from
    a registrar, and stripping every column blindly would risk silently
    accepting a genuinely malformed code that happened to be padded rather
    than corrected. The domain invariant that a name must already be stripped
    (``InstrumentMasterEntry.__post_init__``) is left exactly as strict as it
    was: this is where a caller that builds one directly, bypassing this
    adapter, is still held to it.
    """
    from datetime import date  # noqa: PLC0415 - keeps runtime imports local to parsing

    field = "instrument_token"
    try:
        instrument_token = int(_field(row, field))
        field = "exchange_token"
        exchange_token = int(_field(row, field))
        field = "tradingsymbol"
        trading_symbol = _field(row, field)
        field = "name"
        name = _field(row, field).strip()
        field = "expiry"
        expiry_text = _field(row, field)
        expiry = date.fromisoformat(expiry_text) if expiry_text else None
        field = "strike"
        strike = Decimal(_field(row, field) or "0")
        field = "tick_size"
        tick_size = Decimal(_field(row, field) or "0")
        field = "lot_size"
        lot_size = int(_field(row, field) or "0")
        field = "instrument_type"
        instrument_type = _field(row, field)
        field = "segment"
        segment = _field(row, field)
        field = "exchange"
        exchange = _field(row, field)
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise _MalformedRowError(field=field, reason=str(error), row=row) from error

    try:
        return InstrumentMasterEntry(
            instrument_token=instrument_token,
            exchange_token=exchange_token,
            trading_symbol=trading_symbol,
            name=name,
            expiry=expiry,
            strike=strike,
            tick_size=tick_size,
            lot_size=lot_size,
            instrument_type=instrument_type,
            segment=segment,
            exchange=exchange,
        )
    except InvariantViolation as error:
        raise _MalformedRowError(
            field=_infer_invariant_field(str(error)), reason=str(error), row=row
        ) from error


#: Message substrings from ``InstrumentMasterEntry.__post_init__`` mapped to
#: the field each invariant guards, in the order they are checked -- not every
#: ``invariant()`` call there passes a ``field=`` context, so the column is
#: inferred from the stable, already-tested wording of the message itself
#: rather than by changing the domain invariant just to carry one more field.
_INVARIANT_FIELD_HINTS = (
    ("instrument token", "instrument_token"),
    ("exchange token", "exchange_token"),
    ("trading_symbol", "trading_symbol"),
    ("instrument name", "name"),
    ("instrument_type", "instrument_type"),
    ("segment", "segment"),
    ("exchange", "exchange"),
    ("strike", "strike"),
    ("tick size", "tick_size"),
    ("lot size", "lot_size"),
)


def _infer_invariant_field(message: str) -> str:
    """Best-effort column name for a domain invariant that failed post-parse."""
    lowered = message.lower()
    for needle, field in _INVARIANT_FIELD_HINTS:
        if needle in lowered:
            return field
    return "unknown"  # pragma: no cover - defensive; every current invariant matches above


def _field(row: dict[str, str | None], field: str) -> str:
    """Read one CSV field, allowing blank exactly where the schema permits it.

    A blank ``strike``/``tick_size``/``lot_size`` becomes ``""`` here and is
    turned into the domain's own zero by the caller -- non-tradeable
    instruments (indices) legitimately carry none of the three. A blank
    ``name`` or ``expiry`` stays blank. Every other column still requires a
    genuinely present value: a blank ``exchange``, ``segment`` or
    ``instrument_type`` is unresolvable, not optional.
    """
    value = row[field]
    allow_empty = field in _BLANK_AS_ZERO or field in _BLANK_ALLOWED
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
