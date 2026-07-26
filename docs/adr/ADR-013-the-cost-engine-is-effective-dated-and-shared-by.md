# ADR-013 — The Cost Engine is effective-dated and shared by all three execution modes

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Indian transaction charges change by circular. STT and stamp duty have both changed regime within the span of history this platform will backtest over, and GST components vary by the counterparty's state.

## Decision

Charge rules are versioned rows with `[valid_from, valid_to)`. A cost calculation is always resolved for the trade's own date. Backtest, paper, and live call the identical service.

## Rationale

Rates change by circular; STT and stamp duty have both changed regime. A cost engine that only knows today's rates silently falsifies history.

## Consequences

Cost Engine (S12) is built in Phase 1, early, because everything downstream depends on it.
