# ADR-065 — Consumers deduplicate through a run-scoped ledger written in their own transaction

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

ADR-062 delivers at-least-once, so every consumer will eventually see a duplicate.
Effectively-once processing requires deduplication, and where that check lives
determines whether it actually works.

## Decision

A `processed_event` table with primary key **`(consumer_group, event_id, run_id)`**.

The consumer writes its ledger row **in the same transaction as its side
effects**. A duplicate violates the primary key, the whole unit of work rolls
back, and the side effect does not happen twice.

`run_id` identifies one live session or one replay run. Live uses a constant;
each replay allocates a fresh one.

Ledger rows are pruned after 30 days by a scheduled job; replay runs prune on
completion.

## Rationale

**Not Redis-side deduplication.** It puts the dedup decision in a different
transaction from the work it protects, so a crash between them re-introduces
precisely the duplicate it was meant to prevent. Only a ledger in the consumer's
own transaction composes with the ADR-053 Unit of Work.

**`run_id` is in the key because of replay.** Keyed only by
`(consumer_group, event_id)`, a backtest replaying the same events a second time
would find every row already present and process nothing. It would report zero
trades -- and a zero that looks like a result is worse than an error.

## Consequences

Consumers must run inside a Unit of Work. A consumer whose side effect is not
transactional -- sending an order to a broker -- is **not** protected by this
mechanism, and ADR-015's client-generated idempotency key is what protects it.
The two layers are deliberate: a duplicate event and a duplicate order need
different remedies.

The ledger grows with event volume and needs its retention job from day one.

The live path carries a `run_id` column it barely uses, in exchange for
backtesting that works. That is the trade, stated plainly.
