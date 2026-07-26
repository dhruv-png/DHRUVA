# ADR-023 — Monorepo

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Six deliverable areas -- backend, frontend, mobile, infrastructure, documentation and research -- have to live somewhere, with one engineer routinely making changes that cut across several of them.

## Decision

`backend/`, `frontend/`, `mobile/`, `infra/`, `docs/`, `research/` in one repository with a single CI pipeline and unified versioning.

## Rationale

Solo team. Atomic cross-cutting changes beat repository ceremony.

## Consequences

CI must use path filters to keep feedback fast.
