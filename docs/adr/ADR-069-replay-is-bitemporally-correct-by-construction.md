# ADR-069 — Replay is bitemporally correct by construction: as_of is structural, not a filter

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

ADR-007 requires bitemporal records: `occurred_at` for when a fact was true,
`recorded_at` for when the platform learned it. S04 stores both on every outbox
row.

A replay that selects events by `occurred_at` alone will hand a strategy a
correction that arrived days later. The backtest then trades on knowledge it did
not have. The result is not merely wrong -- it is **optimistically** wrong, which
is the direction that gets capital committed.

## Decision

The replay adapter of `EventStream` takes an **`as_of` at construction** and is
structurally incapable of returning an envelope whose `recorded_at` exceeds it.
The bound is applied in the query, not by a caller.

`recorded_at` is set by the application from the **injected `Clock`** (ADR-011),
never by a database default, so a replay controls it.

Retry and scheduling delays are computed against the injected clock. No
`asyncio.sleep` appears in any retry or scheduling path.

Correlation and causation identifiers are derived deterministically in replay --
UUID5 over `(run_id, sequence)` -- as `InstrumentId.deterministic` already does
in S03.

## Rationale

Look-ahead bias is the failure that invalidates a backtest silently. A filter a
caller may forget is not protection; a constructor parameter enforced in the
query is. The difference is whether correctness is a habit or a property.

`recorded_at` from a database default would put the value outside the replay's
control and make the `as_of` bound unenforceable, so the column carries no server
default and a test asserts a frozen clock produces the frozen value.

Wall-clock sleeps would make a replay of one year take weeks, and would make its
duration depend on how many failures occurred -- nondeterministic wall-time for a
deterministic computation.

Deterministic identifiers matter because ADR-018 requires a signal to emit an
`EvidenceBundle` naming its provenance. Two replays of identical input must
produce diffable evidence, or a result cannot be compared against itself.

## Consequences

Strategy decisions must not depend on correlation or causation values, and a test
asserts it.

The replay adapter cannot be a thin wrapper over the live one; it has a genuinely
different query. That is the cost of the guarantee.

The replay engine itself is deferred (TD-S05-4). This ADR fixes the contract it
must satisfy, so that building it later is implementation rather than
redesign.
