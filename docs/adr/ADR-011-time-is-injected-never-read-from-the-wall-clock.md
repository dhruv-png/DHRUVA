# ADR-011 — Time is injected, never read from the wall clock

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

ADR-010 requires the same code to run under simulated and real time. Deterministic testing requires the same property. Wall-clock reads defeat both.

## Decision

A `Clock` port is injected everywhere. `datetime.now()` is banned outside the clock adapters; enforced by lint.

## Rationale

Prerequisite for ADR-010 and for deterministic tests.

## Consequences

Every service takes a clock dependency. Tests use a frozen clock.
