# ADR-053 — The Unit of Work owns the transaction; repositories never commit

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review.

## Context

Something must decide where a transaction begins and ends. If repositories
decide, then a caller's atomicity depends on which repository methods it happened
to call — and nobody can reason about it from the use case alone.

## Decision

The Unit of Work owns transaction lifetime. Repositories never call `commit`,
`rollback` or `begin`.

* **One use case, one Unit of Work, one transaction.** Not one per request: a
  request may run several use cases, and each is independently atomic.
* **Nesting is refused, not joined.** Silently joining an outer transaction means
  an inner `commit()` does nothing while appearing to succeed, and the caller
  cannot tell the difference.
* **Domain events publish after commit, never inside it.** Publishing inside
  means a consumer can observe an event for a transaction that later rolls back.
* **Any exception rolls back everything.** No partial commit, ever.

Session lifetime is bounded by the Unit of Work and is never global.

## Rationale

Refusing nesting rather than joining is the part worth defending. Joining is more
convenient and is what most frameworks do. It is rejected because the failure is
silent: code that believes it committed, did not, and continues as though it had.
In a system that places orders, that class of failure is not acceptable for the
convenience it buys.

## Consequences

Use cases become the unit of atomicity, which makes them the natural place to
reason about consistency.

A use case needing two independent transactions must open two Units of Work
explicitly. That verbosity is the point: two transactions is a design decision,
not an implementation detail.
