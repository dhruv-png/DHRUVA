# ADR-005 — Money is `Decimal`; storage is integer paise

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Cost accounting accurate to the paisa is one of three stated differentiators (plan section 1.1). The representation of money is chosen once, in S03, and then appears in every context.

## Decision

All monetary values use a `Money` value object wrapping `Decimal`, persisted as `BIGINT` paise + currency code. Floats are permitted **only** inside numerical analytics vectors that never represent settled cash.

## Rationale

Float arithmetic on money is a correctness defect, not a rounding nuisance — and cost accounting is a core differentiator.

## Consequences

`Money`, `Quantity`, `Price`, `BasisPoints` are built in S03 before anything uses them.
