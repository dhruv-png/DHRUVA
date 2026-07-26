# ADR-017 — Broker state is the source of truth; internal state is a hypothesis

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

The user can trade manually, the broker can auto-square-off a position, and fills can arrive partially or late. Internal position state will diverge from the broker's; the only question is whether the divergence is detected.

## Decision

A Reconciliation Service compares internal positions/orders/funds against the broker on a schedule and at every session start. Divergence raises a **CRITICAL** alert and trips the trading kill switch.

## Rationale

Manual trades, partial fills, auto square-offs, and broker-side corrections will desynchronise state. Assume divergence, detect it fast.

## Consequences

Reconciliation is part of the Portfolio Engine (S24) and blocks G5.
