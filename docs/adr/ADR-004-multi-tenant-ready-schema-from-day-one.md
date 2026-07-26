# ADR-004 — Multi-tenant-ready schema from day one

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

v1 serves exactly one user. Multi-tenancy is deferred but explicitly not excluded (plan section 1.2). The schema is being designed now, and schema is the most expensive thing in the system to change later.

## Decision

Every domain table carries `account_id NOT NULL`. No global mutable state, no module-level caches keyed without account. PostgreSQL RLS policies authored from the start, permissive in v1.

## Rationale

Retrofitting tenancy is a full rewrite of every query.

## Consequences

Minor v1 verbosity. Multi-tenant activation (S44) becomes configuration, not migration.
