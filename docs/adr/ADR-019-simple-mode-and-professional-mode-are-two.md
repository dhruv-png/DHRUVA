# ADR-019 — Simple Mode and Professional Mode are two compositions of one API

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Simple Mode and Professional Mode differ sharply in information density and vocabulary, which invites building them as two stacks with two sets of endpoints.

## Decision

No mode-specific backend. Simple Mode is a distinct presentation and a *content policy* (plain-English renderers, traffic-light semantics), driven by the same contracts. Accessibility (WCAG 2.2 AA) is a baseline for both, not a third mode.

## Rationale

Two backends means two sets of bugs and guaranteed divergence.

## Consequences

Every API response carries both machine values and a `plain_english` rendering hint; text generation is server-side and testable.
