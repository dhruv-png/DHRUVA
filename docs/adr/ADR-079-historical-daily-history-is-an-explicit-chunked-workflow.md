# ADR-079 - Historical daily history is an explicit chunked workflow

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Product Owner (Dhruv), CTO / Principal Architect
- **Supersedes:** -
- **Superseded by:** -

> Origin: approved P0 Historical Market, Benchmark and Corporate-Action
> Foundation slice on the personal MVP critical path.

## Context

The routine market-data refresh deliberately fetches at most twenty calendar
days for the current owner watchlist and commits one synchronized batch. That
is the correct failure boundary for daily operation, but it cannot safely be
widened into a multi-year job. Later 20/60/120/200/252-session features need
deeper history, while evaluation needs materially more depth and a historical
universe that is not selected from today's survivors.

Existing daily-bar revisions already separate trading date from the UTC
retrieval/knowledge instant, retain response and source provenance, record an
explicit adjustment status, and append idempotently. Zerodha history currently
reports `UNKNOWN` adjustment semantics. The archived NIFTY 50 mapping exposes a
price index; there is no approved NIFTY 50 TRI or corporate-action source.

## Decision

Historical acquisition is a separate explicit workflow. `backfill-plan` is
network-free and requires exactly one bounded `--years` or `--from` target.
`backfill` executes only that locally derived plan. Routine `refresh` keeps its
twenty-calendar-day configuration ceiling and whole-watchlist atomicity.

Plans are provider-neutral application values. Targets are ordered benchmark
first and then owner instruments by canonical symbol. Each target is divided
newest-to-oldest into 365-calendar-day core chunks with seven calendar days of
forward overlap. A transaction contains one target plus the benchmark, or the
benchmark alone. Provider fetches remain sequential. A successful chunk commits
independently; a later refusal leaves earlier chunks intact, and a rerun derives
the remainder from the earliest locally stored session. Content-derived source
revisions make overlaps and retries idempotent.

The overlap makes close discontinuities at chunk boundaries observable. Every
two-series chunk must still match the benchmark's returned sessions exactly.
Malformed responses, mixed providers, unexplained close moves greater than the
existing quality bound, and session mismatches fail before that chunk writes.

Historical market dates retain the provider adapter's actual modern
`retrieved_at`; they are reconstructed historical facts, not facts DHRUVA knew
prospectively. Frozen research observations are never revisited or rewritten.

Coverage derives the full visible stored span and exact session counts. It
reports feature thresholds at 20, 60, 120, 200 and 252 sessions, an operational
252-session gap, and a stronger 2,000-session acquisition-depth target. Meeting
2,000 sessions does not itself mean a model is validated.

The current benchmark is labelled `PRICE_INDEX`, never TRI. Zerodha bars remain
`UNKNOWN` adjustment status and raw observations are never destructively
rewritten or presented as total returns. A licensed TRI and corporate-action
source may later complement the provider-neutral contracts through a new
decision. No NSE scraping or fabricated adjustment is permitted.

## Rationale

One multi-year transaction would be expensive to retry and unsafe to interrupt.
One transaction per individual provider request would lose the synchronized
benchmark invariant. The chosen pairwise chunk is the smallest useful atomicity
unit that preserves that invariant and permits deterministic partial progress.

A durable checkpoint table was rejected because stored append-only coverage is
already the authoritative checkpoint. A migration solely for derived coverage,
benchmark labels, or a transient plan would duplicate facts and add unnecessary
state.

## Consequences

- Alembic remains at `0018_research_observation`; no schema or RLS change is
  required.
- Planning can be reviewed without credentials or provider traffic, including
  exact request counts and mapping blockers.
- A stock with genuine missing sessions, an empty pre-listing response, or a
  possible corporate-action discontinuity can stop its chunk. The operator must
  resolve the provenance gap; DHRUVA does not invent data to continue.
- Deepening today's twenty stocks enables feature development but does not
  create an unbiased historical evaluation universe. Historical constituent
  membership and delisted instruments remain a later licensed-data problem.
- Total-return evaluation remains blocked until an approved TRI and auditable
  corporate-action source exists.
