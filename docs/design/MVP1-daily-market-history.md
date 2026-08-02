# MVP 1 daily market history

## Purpose

This slice retrieves and persists daily cash-equity and Nifty 50 candles through
the read-only Kite boundary established by ADR-076. It implements the raw market
history required by the personal MVP without adding orders, positions, holdings,
continuous futures or corporate-action adjustment.

Official contracts reviewed on 2 August 2026:

- <https://kite.trade/docs/connect/v3/historical/>
- <https://kite.trade/docs/connect/v3/exceptions/#api-rate-limit>

Kite documents `GET /instruments/historical/:instrument_token/day`, six-value
OHLCV rows, an optional seventh OI value, and a historical-candle limit of three
requests per second. DHRUVA serializes one adapter instance at a conservative two
requests per second. Requests are limited to 1,900 calendar days and responses to
8 MiB or 2,000 candles. Wider backfills must be chunked and resumed by the future
job orchestrator rather than weakening these bounds.

## Data contract

`DailyHistoryRequest` carries the stable DHRUVA instrument id, dated provider
token, instrument kind, inclusive date range and explicit futures-only flags.
The provider response retains exact bytes and SHA-256 in memory. Every persisted
bar revision records:

- stable instrument id and instrument kind;
- trading date and exact decimal OHLC;
- volume and optional open interest;
- provider and then-current provider token;
- retrieval time and source/batch revisions;
- adjustment status and completeness; and
- immutable quality-validator revision.

Kite's documentation does not prove whether historical prices have been adjusted
for every corporate action. The adapter therefore records `UNKNOWN`, never
guesses `RAW` or `ADJUSTED`. Point-in-time reads reject a series that mixes
adjustment states. ADR-008's adjustment-aware read path remains future work and
will require official corporate-action evidence.

## Quality validator v1

`daily-history-quality-v1` rejects:

- duplicate or unsorted dates;
- non-positive or non-finite prices;
- impossible high/low relationships;
- negative volume or open interest;
- candles outside the requested range;
- a missing or incomplete required-through session;
- any cash/index session set that differs from the Nifty benchmark set; and
- an unexplained close-to-close move greater than 35%.

The 35% screen is deliberately conservative. It does not claim that a corporate
action occurred; it refuses to let a likely split, bonus or other discontinuity
silently become strategy input while adjustment state is `UNKNOWN`. The future
corporate-action reconciliation path may explain and version the series. It must
not bypass or mutate this evidence.

A bar after the caller-supplied completed-through date is persisted as
`INCOMPLETE` for diagnosis, but complete-series queries reject it. Market
sessions are not inferred from weekdays: the explicitly requested Nifty series
is the session calendar for the synchronized batch.

## Persistence and replay

Migration `0015_daily_market_bars` adds append-only
`daily_market_bar_revision`. A normalized bar-content hash makes identical
provider retries idempotent even when retrieval range, batch bytes or current
provider token changes. Corrections append a new revision. Queries select the
latest revision whose retrieval time is no later than `known_at`, preventing a
later correction from leaking into an earlier backtest.

The table remains plain PostgreSQL. At the approved maximum of 50 end-of-day
symbols, hypertable compression and retention would add operational policy with
no material benefit. Writes still use an explicit primitive bulk path governed
by ADR-054 and participate in the Unit-of-Work transaction. No retention policy
or destructive migration is introduced.

## Known limits and next step

- Ordinary tests use sanitized JSON and do not require credentials.
- The first real Kite smoke test remains explicit and credential-gated.
- Corporate-action verification and read-time adjustment are not implemented.
- This slice does not fetch actual futures history or OI; the domain already
  preserves an optional OI field for that next vertical extension.
- Freshness quotes remain separate from historical completeness.

Next, add actual futures daily OHLCV/OI ingestion using the archived contract
revisions, followed by deterministic continuous research-series construction.
