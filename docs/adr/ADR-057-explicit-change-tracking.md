# ADR-057 — Explicit change tracking with optimistic concurrency

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review.

## Context

ADR-052 detaches domain objects from the session, which gives up SQLAlchemy's
automatic dirty checking. Something must replace it.

| Option | Assessment |
|---|---|
| Snapshot on load, diff on commit | Automatic, but doubles memory per aggregate and deep-compares on every commit |
| Version column only | Detects conflicts, not changes |
| **Explicit `repository.update()`** | **Chosen.** Verbose and completely predictable |

## Decision

Persistence is explicit. `repository.update(aggregate)` is required for a change
to be written; nothing is inferred.

Every aggregate carries a `version` column. Updates execute
`UPDATE ... WHERE id = :id AND version = :loaded_version`; zero rows affected
means another writer won, and the caller receives a typed `ConflictError`.

## Rationale

The failure mode is what decides this. Implicit tracking fails **silently** — a
mutation that is simply never persisted, discovered later as missing data with no
error anywhere. Explicit tracking fails **noisily** — a forgotten `update()` call,
which a use-case test catches immediately.

In a system that records money movements, a noisy failure is worth a great deal
of verbosity.

Optimistic locking on every aggregate rather than only where contention is
expected: the cost is one integer column and one WHERE clause, and retrofitting
concurrency control to a live table under load is not something to plan for.

## Consequences

Use cases read as load → mutate → update → commit, which is more verbose than
load → mutate → commit and states exactly what happens.

`ConflictError` becomes a case callers must handle on any contended aggregate.
Retry policy belongs to the caller; the repository only reports the conflict.
