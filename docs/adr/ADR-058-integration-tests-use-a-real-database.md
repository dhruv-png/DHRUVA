# ADR-058 — Integration tests run against real PostgreSQL with TimescaleDB

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review.

## Context

SQLite would make the integration suite fast and portable. It would also make it
lie: no `TIMESTAMPTZ` semantics, different `BIGINT` overflow behaviour, no
hypertables, different transaction isolation, different constraint timing, no
`NUMERIC` exactness guarantees.

A suite that passes on SQLite and fails on PostgreSQL is worse than a slower one
that tells the truth, because it converts a caught bug into a deployed one.

## Decision

Integration tests run against **real PostgreSQL with the TimescaleDB extension**,
provisioned by testcontainers. No SQLite, no in-memory substitute, no mocked
database for anything asserting transactional behaviour.

* Container is **session-scoped** — starting it once is the expensive part.
* Each test runs in a **transaction rolled back at teardown**, so tests are
  isolated and order-independent without re-creating the schema.
* Tests that must observe committed state open their own connection explicitly,
  and clean up explicitly.

Unit tests remain database-free. Anything that can be tested without a database
is, and the integration suite covers only what genuinely requires one.

## Rationale

The cost is a slower suite and a Docker dependency. Both are accepted, because
the failure this prevents — transactional behaviour that differs between test and
production — is the exact class of bug an integration test exists to catch.

## Consequences

Integration tests cannot run where a container runtime is unavailable. That is a
real limitation and must be recorded as a gap rather than papered over with
mocks: an unrun test is honest, a mocked one is misleading.

CI must provide Docker. Local development without it can run the unit suite,
which is the majority of the tests but not the ones that verify this subsystem.
