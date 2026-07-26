# ADR-052 — Separate persistence models with an explicit mapping layer

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review.

## Context

The domain model must not know how it is stored. Three ways to achieve that with
SQLAlchemy, in increasing order of separation:

**Active Record** — domain objects with `.save()`. Rejected outright: every
domain object would depend on a session.

**Imperative mapping** — `registry.map_imperatively()` attaches persistence to
domain classes without those classes importing SQLAlchemy. This is the pattern
*Architecture Patterns with Python* recommends, and it removes the mapping layer
entirely.

**Separate models plus explicit mappers** — two class hierarchies, joined only
by mapping functions.

## Decision

Separate persistence models, with an explicit mapping layer.

Imperative mapping is rejected despite being cheaper. It leaves the domain object
**instrumented at runtime**: carrying `_sa_instance_state`, participating in the
identity map, exhibiting lazy-loading behaviour, and behaving differently
depending on whether a mapper has been configured in the current process.

The import graph would look clean. The runtime object would not be. That is
persistence coupling that no static tool can see, and it is the harder kind to
notice — a `Money` acquiring hidden state, a test passing or failing depending on
import order.

The separation must therefore hold **both statically and at runtime**.

## Rationale

The cost is real and was weighed: a model, a mapper and mapper tests per
aggregate, plus a function call and an allocation per row.

For aggregates that cost is irrelevant — orders and instruments are counted in
thousands, not millions. For timeseries it would be fatal, which is why ADR-054
carves out a bounded exception rather than compromising this decision.

## Consequences

Every aggregate needs a model, a mapper, and a round-trip property test asserting
`to_domain(to_model(x)) == x`.

SQLAlchemy's automatic dirty tracking is lost, which forces the explicit change
tracking in ADR-057. That is a consequence, not an accident.

Mapper modules are the only ones importing both worlds, which makes rule R7
checkable: a `domain/` package importing SQLAlchemy fails the build.
