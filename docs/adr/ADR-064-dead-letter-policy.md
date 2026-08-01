# ADR-064 — Dead-letter policy: classified failures, bounded retries, explicit replay

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

A publication can fail because the broker is briefly unreachable, or because the
envelope is malformed. Treating those identically wastes ten minutes on the
second case and gives up too early on the first.

S04 already declares `MAX_DELIVERY_ATTEMPTS = 12` and provides `attempts` and
`next_attempt_at`.

## Decision

Failures are **classified** using the ADR-038 taxonomy:

- **Retryable** -- broker unreachable, timeout, lock contention. Retried with
  exponential backoff and full jitter: `min(base * 2^attempts, cap)`, base 1s,
  cap 60s, up to **12 attempts** (~10 minutes).
- **Terminal** -- unknown `event_version`, malformed payload, failed validation.
  Routed to the dead-letter queue **immediately**, consuming no retries.

The **dead-letter queue is a PostgreSQL table**, not a Redis stream.

Replay from the DLQ is **explicit and human-initiated**. DLQ depth is a monitored
metric with a non-zero alert threshold.

## Rationale

**12 attempts, not 5.** S04 already declared 12, and roughly ten minutes of
retry is a defensible survival window for a broker blip. The tuned parameter is
the cap, not the count.

**Jitter is not decoration.** Without it, an outage produces a synchronised retry
storm at recovery, which is how an outage extends itself.

**PostgreSQL, not Redis, for the DLQ.** A dead-letter queue that a `FLUSHALL` or
an eviction can erase is the worst possible home for exactly the events that most
need attention. It also makes DLQ contents queryable in SQL and durable across a
Redis wipe, at the cost of a second mechanism -- which is the right trade for
data whose defining property is that it needs a human.

**No automatic drain.** A message reached the DLQ because retrying did not help.
Retrying it on a timer is a loop, not a recovery.

## Consequences

A poison message is moved aside, never retried in place, so it cannot block the
head of the stream. One malformed event must not stop the platform.

Backoff delays are computed against the injected `Clock` (ADR-011), never slept
against the wall clock -- otherwise a replay of a year of data would sleep for
weeks and its duration would depend on failure counts.

The DLQ needs its own retention and access control: its contents are as sensitive
as the events themselves.
