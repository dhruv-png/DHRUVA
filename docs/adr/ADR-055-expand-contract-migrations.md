# ADR-055 — Expand/contract migrations; every migration declares its reversibility and operational impact

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review, extended by the Design Review.

## Context

Up to S03, rollback meant `git checkout`. From S04 it means code **plus schema**,
and the two have different guarantees. Restoring a dropped column restores the
column, not the data that was in it.

## Decision

**Destructive changes ship across multiple releases** using expand/contract. A
column rename is: add new → backfill → write both → read new → stop writing old →
drop old, in separate releases. Each step is independently reversible; a
single-release rename is not.

**Every migration declares, in a structured docstring header:**

| Field | Content |
|---|---|
| Reversibility | `reversible` or `irreversible: <reason>` |
| Rollback procedure | The exact steps, or why none exists |
| Irreversible operations | Named explicitly, or `none` |
| Expected runtime | Order of magnitude, at production data volume |
| Operational impact | Locks taken, whether it blocks writes, whether it needs a maintenance window |

Silence is not permitted. A migration with no declaration fails the migration test
harness.

**CI validates `upgrade head` → `downgrade base` → `upgrade head`** against a
real database wherever technically possible, and asserts that
`alembic --autogenerate` produces an empty diff so models and migrations cannot
drift.

## Rationale

The four fields beyond reversibility were added at Design Review, and they are
the ones an operator actually needs at 09:20 on a trading day. "Is it reversible"
is the question you ask while planning; "does it take a lock and for how long" is
the question you ask while deciding whether to run it now.

Requiring the declaration even when the answer is trivial is deliberate. "No
locks, sub-second, reversible" written down is *verified trivial*; the same
migration with no header is *nobody checked*.

## Consequences

Migrations are more verbose, and a rename takes three releases instead of one.

Some migrations will be marked irreversible. That is an honest outcome, and the
release notes must carry it rather than implying a clean rollback.

Where the up→down→up cycle cannot run — no database available in an environment —
that is recorded as a gap rather than silently skipped.
