# ADR-059 — Boundary rules R7 and R8: the domain imports no persistence, the timeseries path imports no domain

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review, with R8 added by Amendment 1.

## Context

ADR-052 and ADR-054 each depend on a direction of dependency that no reviewer can
reliably police by hand across forty subsystems.

## Decision

**Rule R7 — the domain imports no persistence framework.** The build fails if any
module under a `domain/` package imports `sqlalchemy`, `alembic`, `asyncpg` or
`psycopg`.

**Rule R8 — the timeseries path imports no domain.** The build fails if any
module under a `timeseries` infrastructure package imports from any `domain`
package. A business entity cannot be named on that path, so it cannot be
persisted through it.

R7 protects a *layer* from a dependency. R8 protects a *dependency* from a layer.
Together they make ADR-052 and ADR-054 checkable rather than aspirational.

## Rationale

Rule R6 established the pattern: where a decision matters and is easy to violate
in one convenient line, it becomes a build rule. Both of these qualify.

R8 in particular exists because ADR-054 creates a second persistence path, and a
second path is exactly the kind of thing that gets used opportunistically. The
rule makes opportunistic use impossible rather than discouraged.

## Consequences

Repository *interfaces* are declared in the domain as protocols with no framework
types in their signatures. Implementations live in infrastructure.

A domain object needing to be written to a timeseries table must be decomposed
into columns by something outside the timeseries package — which is the correct
place for that translation, and now the only possible one.
