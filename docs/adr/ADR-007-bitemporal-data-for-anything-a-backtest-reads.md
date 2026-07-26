# ADR-007 — Bitemporal data for anything a backtest reads

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Backtests must not see data before it was knowable. FII/DII figures are revised, fundamentals are restated, and corporate actions are corrected after the fact. Each is a route by which the future leaks into the past.

## Decision

Records carry both `event_time` (when it was true in the market) and `recorded_at` (when we learned it). Backtests query *as-of* `recorded_at`.

## Rationale

The only reliable structural defence against lookahead bias. Restated fundamentals, revised FII/DII figures, and late-corrected corporate actions all leak the future otherwise.

## Consequences

Slightly heavier schema and every ingestion path must set both. Non-negotiable.
