# ADR-014 — Orders and fills are an append-only immutable ledger

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Order state is needed for reconciliation, audit and post-mortem debugging, and the SEBI framework requires order-level traceability (plan section 2.2). Event sourcing provides this but is costly to apply broadly.

## Decision

Event-sourced within the Trading context only. Current position/order state is a projection. Nothing is ever updated in place or deleted.

## Rationale

Audit trail is a regulatory and debugging necessity (see §2.2). Reconciliation requires history.

## Consequences

Event sourcing is used *only* here. Every other context is ordinary CRUD. Deliberate asymmetry.
