# ADR-061 — The event envelope is versioned, self-describing and transport-agnostic

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

ADR-002 fixed the envelope fields: `event_id`, `event_type`, `event_version`,
`occurred_at`, `recorded_at`, `account_id`, `correlation_id`, `causation_id`,
`payload`. It did not specify how they are encoded, how versions are negotiated,
or how domain primitives cross the wire.

The S04 outbox carries six of those nine columns. Encoding was left to a
placeholder that serialises dataclass fields with no version and no contract.

Money is the reason this matters. ADR-042 makes money an exact integer count of
minor units, and S03 spends real effort proving a value survives a round trip
exactly. A float appearing in an envelope would undo that at the one point where
nobody is looking, because a serialiser is the last place anyone thinks to check.

## Decision

The envelope adds `aggregate_type` and `sequence` to ADR-002's nine fields, and
is encoded as **canonical JSON**: sorted keys, `(",", ":")` separators, UTF-8, no
non-finite values, and **no float anywhere**.

Domain primitives serialise by their exact representation:

    Money      -> {"minor_units": 123456, "currency": "INR"}
    Price      -> {"scaled_units": 123456780000, "scale": 8}
    Quantity   -> {"units": 50}
    Decimal    -> {"decimal": "1.2345"}       (string, never float)
    UUID       -> canonical hyphenated string
    datetime   -> RFC 3339, timezone-aware, UTC

`event_version` is an integer on the envelope, starting at 1. Additive optional
fields do not bump it; removals, renames and meaning changes do. A consumer
declares the versions it understands, and an envelope at an unknown version is
routed to the dead-letter queue rather than skipped or guessed at (ADR-022).

## Rationale

**JSON, not msgpack or protobuf.** Bus payloads are domain events, not ticks --
ticks take the ADR-054 bypass and never reach the bus. When an envelope is stuck
in a dead-letter queue at 09:20 on a trading day, reading it without tooling is
worth more than the bytes a binary codec saves.

**Canonical JSON specifically**, because replay determinism requires that the
same event serialises to the same bytes on every run. Python dict ordering and
default separators make that fragile unless it is pinned. This costs nothing now
and cannot be retrofitted once events are stored.

**No schema registry.** It is a second source of truth and an operational
dependency. A version integer plus a consumer-side check gives the same
protection at this scale. Revisit when external consumers exist.

## Consequences

Every domain primitive needs an explicit codec, and a type without one fails to
serialise rather than degrading to `str()`. That is the intended failure.

Envelopes are hashable and byte-comparable, which is what makes the determinism
tests in the S05 test strategy possible.

Boundary rule R6 bans float in the monetary modules but does not reach
serialisation code. Recorded as TD-S05-1; the codec's own tests carry the
guarantee until a rule does.
