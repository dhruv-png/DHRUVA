# ADR-001 — Modular monolith, not microservices

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

A solo engineer must ship a platform spanning nine bounded contexts. The approved stack list includes Kubernetes and the team size is one. A deployment topology has to be chosen before any code is laid out, and the choice is expensive to reverse in either direction.

## Decision

Single codebase, 4 processes, build-time-enforced context boundaries via `import-linter`. Service extraction only when a context has a demonstrated independent scaling or availability need.

## Rationale

Solo team. Distributed systems cost is organisational, not technical.

## Consequences

Kubernetes deferred to P6. Docker Compose is the deployment unit through P5.
