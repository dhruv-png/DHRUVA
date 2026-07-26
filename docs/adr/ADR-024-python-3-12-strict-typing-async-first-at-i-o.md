# ADR-024 — Python 3.12, strict typing, async-first at I/O boundaries

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

This codebase will outlive its author's memory of it and has exactly one maintainer (risk R15 in the plan's register). Something other than that maintainer has to hold the invariants.

## Decision

`mypy --strict` on `backend/src`, zero `# type: ignore` without a linked issue. `async` for all network and DB I/O; CPU-bound numerics run in worker processes, never on the event loop.

## Rationale

A long-lived codebase with one maintainer needs the compiler to be the second maintainer.

## Consequences

CI fails on type errors. No exceptions, no gradual-typing escape hatch.
