# ADR-008 — Prices stored unadjusted; adjustment applied at read time

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Corporate actions change the meaning of a historical price series. The adjustment can be applied destructively at write time, which is simple and fast, or non-destructively at read time, which is neither.

## Decision

Raw exchange prices are immutable. A separate `corporate_action` + derived `adjustment_factor` series is applied by the read layer.

## Rationale

Destructive back-adjustment makes historical data unauditable and un-recomputable after a correction.

## Consequences

All price reads go through an adjustment-aware repository. Direct table reads in analytics code are banned.
