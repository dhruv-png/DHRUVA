# ADR-026 — No live capital before Gate G5

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

The execution path is the only part of the platform that can lose money. The temptation to validate it with 'just a small live position' is predictable, and arrives precisely when judgement is worst.

## Decision

Live broker credentials with order permissions are not configured in any environment until Gate G5 passes, which requires a signed paper-trading parity report (§8, G5).

## Rationale

The most likely way this project loses money is deploying an unvalidated execution path.

## Consequences

Paper trading (S32) must be good enough to be a genuine validation, not a demo.
