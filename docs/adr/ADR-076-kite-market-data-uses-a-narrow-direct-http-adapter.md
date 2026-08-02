# ADR-076 — Kite market data uses a narrow direct HTTP adapter

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Personal MVP Plan v1.0, MVP 1. The owner approved read-only Zerodha
> Kite Connect at up to ₹500 per month and explicitly prohibited broker orders.

## Context

MVP 1 needs the daily instrument master, historical candles, freshness quotes,
open interest and contract metadata. The official Python client is MIT-licensed
and maintained, but exposes orders, modifications, cancellations, GTT, holdings
and positions alongside market data. It is synchronous and adds a second HTTP
stack. None of those extra capabilities belong in DHRUVA's private research and
paper-trading boundary.

Kite's official v3 HTTP documentation publishes the required read endpoints,
authentication header, CSV schema, gzip behavior and quote limits. The existing
dependency set already pins Pydantic's BSD-3-Clause `httpx2` async client. Moving
that same pin from development-only to runtime introduces no new package version
and permits an injected transport for deterministic tests.

Sources reviewed on 2 August 2026:

- <https://kite.trade/docs/connect/v3/market-quotes/>
- <https://kite.trade/docs/connect/v3/historical/>
- <https://kite.trade/docs/connect/v3/user/>
- <https://github.com/zerodha/pykiteconnect>
- <https://pypi.org/project/httpx2/>

## Decision

The Reference infrastructure owns a narrow direct-HTTP Kite adapter. Its public
surface contains read-only provider ports and provider-neutral domain values;
Kite URLs, headers, CSV fields and HTTP types do not cross into application or
domain code.

The first adapter represents only documented `GET /instruments`. It sends Kite
API version 3, accepts the daily gzip-decoded CSV, bounds response bytes and row
count, parses exact decimals, rejects schema/value drift, archives replayable
bytes with SHA-256, and uses an injected UTC clock. It retries timeouts, transport
failures, 429 responses and 5xx responses at most three times with bounded
backoff. Authentication failures do not retry and never render credentials.

Instrument resolution requires an exact NSE cash symbol and exact controlled
NFO underlying name. Current futures contracts must be `FUT` in `NFO-FUT`, have
an unexpired date, positive lot and tick size, and an unambiguous expiry mapping.
All active months are retained. Missing or ambiguous futures degrade only that
underlying to `CURRENTLY_UNAVAILABLE`; its cash mapping remains usable.

The API key and daily access token are `SecretValue` instances supplied by the
existing credential infrastructure. Ordinary tests use a sanitized committed
CSV and an in-memory HTTP transport. A real smoke test remains explicit and
credential-gated.

## Rationale

Direct HTTP is the smallest safe integration because the required server
contract is public and narrow, while importing an all-capabilities SDK would add
unused live-capital operations and a second HTTP client. A handwritten socket or
TLS client was rejected because it would duplicate mature timeout, connection
pool and decoding behavior. Treating the owner list as permanent futures
eligibility was already rejected by ADR-075.

## Consequences

- Production dependencies now include the already pinned `httpx2==2.9.1`.
- No order, GTT, holdings or positions endpoint exists in DHRUVA source.
- A rejected or expired Kite session becomes `DHR-EXT-005` and requires the
  future authorization operation to refresh it.
- Provider tokens remain effective attributes; actual futures contract identity
  derives from stable underlying, exchange and expiry.
- Historical-candle and quote methods will extend this same read-only adapter
  without widening it to account or live-capital operations.
