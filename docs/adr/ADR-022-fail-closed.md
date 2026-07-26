# ADR-022 — Fail closed

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Many runtime conditions are ambiguous rather than clearly good or bad: stale market data, unknown F&O ban-list state, missing margin figures, unreconciled positions, a degraded broker session.

## Decision

On any ambiguity — stale data, missing margin info, unknown ban-list state, reconciliation mismatch, degraded session — the system **blocks trading** and explains why. It never proceeds on a guess.

## Rationale

In a financial system, the cost of a false block is an inconvenience; the cost of a false proceed is capital.

## Consequences

Every gate has an explicit "unknown" branch that routes to block, and each is unit tested.
