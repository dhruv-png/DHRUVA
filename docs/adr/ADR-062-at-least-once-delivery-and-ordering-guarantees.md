# ADR-062 — At-least-once delivery; per-aggregate ordering live, total deterministic ordering in replay

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

The outbox makes an event durable with the change that caused it. It cannot make
publication atomic with that commit: the relay may publish and then die before
recording that it did.

Separately, S04's outbox docstring claims rows carry a monotonic `sequence` "so
the drainer can publish in commit order". That is false. A PostgreSQL sequence is
monotonic in *allocation*, not in *commit*: transaction A may take 100, B take
101, and B commit first. Any relay tracking a high-water mark would publish 101
and then skip 100 permanently.

## Decision

**Delivery is at-least-once. Exactly-once is not offered.**

**Progress is tracked by `published_at`, never by a cursor or high-water mark.**
A relay claims rows `WHERE published_at IS NULL`, so a late-committing row is
picked up on a later poll instead of being skipped. Cursor-based progress is
forbidden.

**Ordering has two different guarantees for two different purposes:**

| Mode | Guarantee |
|---|---|
| Live delivery | Per `aggregate_id`, in `sequence` order. No cross-aggregate order |
| Replay | Total, deterministic, reproducible across runs |

Live order is a strict subset of replay order, so a consumer correct under live
ordering is correct under replay.

**At-most-once is rejected** outright: it trades correctness for latency the
platform does not need.

## Rationale

Publishing before marking risks a duplicate. Marking before publishing risks
silent loss. **Losing an event about capital is strictly worse than repeating
one**, so the order is fixed: publish, then mark.

Exactly-once across a database and a broker needs distributed consensus this
project is not buying. Systems claiming it usually mean at-least-once plus
deduplication, which is ADR-065. Saying so plainly is the honest engineering.

Global live ordering would require a single stream and a single consumer -- a
throughput ceiling bought for a guarantee almost nothing needs. But a backtest
needs determinism, or a result cannot be reproduced or defended. Giving replay
the stronger guarantee costs nothing live and buys everything in replay.

## Consequences

Every consumer must be idempotent. A consumer that is not is a defect, and
ADR-065 is the mechanism.

A consumer accidentally depending on cross-aggregate interleaving behaves
differently live and in replay -- and the backtest is the one that will look
right. The `ShuffledReplay` conformance mode exists to catch that.

The claim query is slightly more expensive than a cursor scan. That is the price
of not silently skipping rows, and it is worth paying.

Effectively-once processing holds only where a consumer's side effects are
transactional. **It does not hold for external calls such as broker dispatch**,
where ADR-015's client-generated idempotency key is the real protection.
