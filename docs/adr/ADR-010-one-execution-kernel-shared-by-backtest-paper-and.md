# ADR-010 — One execution kernel shared by backtest, paper, and live

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Strategy logic will be exercised in three modes -- backtest, paper and live. Each mode could plausibly have its own runner, and in most quant platforms each does.

## Decision

Strategy code is written once against an abstract `ExecutionContext`. Backtest, paper, and live differ only in the adapter bound to that context and the clock implementation.

## Rationale

Research/production skew is the most common and most expensive failure mode in quant platforms. If the code paths differ, the backtest is fiction.

## Consequences

The Strategy Kernel (S26) must be designed before the Backtester (S30), and both are constrained by it.
