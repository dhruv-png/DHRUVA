# ADR-015 — Idempotency on the entire order path

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Order placement crosses a network. A timeout tells the caller nothing about whether the order reached the exchange, and the naive response -- retry -- is how duplicate positions are created.

## Decision

Every order carries a client-generated idempotency key persisted before dispatch. Retries are safe by construction. Broker responses are matched back by key.

## Rationale

Network timeouts on order placement are the classic path to accidental duplicate positions.

## Consequences

Order placement is a two-phase persist-then-dispatch operation.
