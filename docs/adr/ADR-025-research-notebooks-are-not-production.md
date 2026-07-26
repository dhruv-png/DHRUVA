# ADR-025 — Research notebooks are not production

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Quantitative research is done in notebooks. Notebook code is fast to write, reads global state, is rarely tested, and is exactly what a tired researcher will be tempted to import from production code at 11pm.

## Decision

`research/` holds notebooks and is excluded from type/lint gates, but **no notebook code may be imported by `backend/src`**. Promoting research into production means rewriting it against the domain model with tests.

## Rationale

Notebook code in production is how quant platforms rot.

## Consequences

A deliberate, budgeted translation step for every model. Accounted for in complexity estimates.
