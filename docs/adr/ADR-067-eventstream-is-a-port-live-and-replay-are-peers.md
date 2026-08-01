# ADR-067 — EventStream is a port; live and replay are peer adapters

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

DHRUVA must run one body of strategy code against historical replay, paper
trading and live trading (ADR-010). S05 is where events start moving, and
movement is where "live" normally leaks into consumer code.

## Decision

Two ports in the shared kernel:

- **`EventPublisher`** -- the relay's exit. Redis Streams is one adapter.
- **`EventStream`** -- the consumer's entrance. It yields
  `(envelope, ack_token)`, where `ack_token` is **opaque**.

**Redis Streams and the replay engine are peer adapters behind `EventStream`.**
Neither is privileged. An in-memory adapter exists for tests and development.

The envelope carries **no transport field**. Consumer-group names, stream IDs,
delivery counts and partition keys are transport facts; if one reached a
consumer, replay would have to fabricate it.

Every adapter passes one shared conformance suite.

## Rationale

A port whose only implementation is Redis will be Redis-shaped, and the shape
will only be discovered when the replay adapter is written against it -- by which
time consumers depend on the leak. Building the in-memory adapter first is what
proves the port is not Redis-shaped.

The opaque ack token is the specific defence: a consumer that can inspect it can
branch on it, and a consumer that branches on transport is no longer portable.
The replay adapter's token is a no-op.

## Consequences

An extra indirection between the consumer and Redis, paid on every event. The
budget in the S05 design accounts for it.

The conformance suite is the mechanism that stops the replay adapter drifting
from the live one. Without it, two adapters diverge quietly and the divergence
surfaces as a backtest that does not match production.

S05 builds the port and the in-memory adapter. The replay *engine* is deferred:
building it before any strategy exists would be designing against an imagined
consumer.
