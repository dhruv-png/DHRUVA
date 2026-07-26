# ADR-056 — Async session discipline: bounded, deterministic, never global

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review.

## Context

`AsyncSession` is not concurrency-safe. Sharing one across `asyncio` tasks
produces interleaved-statement errors that are difficult to reproduce and easy to
misdiagnose as database problems.

Lazy loading in an async context is worse: it is implicit IO in a place the author
did not expect, and the N+1 it causes typically appears only at production data
volume.

## Decision

* **One session per Unit of Work.** Never shared across tasks. No module-level or
  otherwise global session.
* **`expire_on_commit=False`.** Expiry would trigger a refresh query after commit
  that nobody asked for, and an error on a detached object.
* **`lazy="raise"` on every relationship.** A lazy load is a bug, not a
  convenience; loading strategy is stated explicitly at the query.
* **Session lifetime is bounded by the Unit of Work** and is deterministic: it
  opens when the UoW opens and closes when the UoW exits, on every path.
* **Pool exhaustion raises a typed error after a bounded wait**, never blocks
  indefinitely. A hung request is harder to diagnose than a failed one.

## Rationale

`lazy="raise"` is the decision most likely to feel obstructive during
implementation, and it is the one worth keeping. The alternative is discovering
at S30 that a backtest issues one query per bar, which is exactly the class of
problem that does not appear in a unit test and does appear under load.

## Consequences

Every relationship traversal requires an explicit `selectinload` or `joinedload`
at the query site. Loading becomes a visible decision.

Pool sizing is configuration (defined in `DatabaseSettings` at S02, first used
here) and needs revisiting once real concurrency exists at S10.
