# ADR-063 — The outbox relay is a dedicated process, not a Celery beat task

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

Something must drain the outbox. Celery and its beat scheduler are already
planned for S05, so reusing them is the obvious economy.

## Decision

The relay is a **dedicated long-running process** running an asyncio loop: claim
a batch, publish, mark published, sleep briefly when idle. It is not a Celery
task and not a beat entry.

Rows are claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, so N relay instances
are safe without coordination.

## Rationale

**Latency.** Beat's practical granularity is seconds to minutes. The relay should
drain within a second of a commit; a one-minute beat would make every event a
one-minute event.

**Starvation.** A relay running as a worker task competes with the pool it feeds.
Under load -- precisely when delivery matters most -- it would queue behind work
its own events created.

**Concurrency control belongs in the query.** `SKIP LOCKED` is a property of SQL.
Depending on beat's singleton behaviour instead would make correctness rest on an
operational assumption.

## Consequences

One more process to deploy, supervise and monitor. It ships with the health,
readiness, metrics and tracing surface ADR-035 requires of every runtime
component, and its unpublished-row age is the platform's delivery lag signal.

Horizontal scaling is free: start another instance.

The relay must not hold a row lock across broker I/O, or a slow broker becomes
lock contention. Claim and mark are short transactions; publication happens
between them.
