# ADR-054 — Bulk timeseries writes bypass the ORM behind a dedicated TimeSeriesStorage interface

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Proposed in the S04 design document and approved at Design Review, with the boundary tightened by Amendment 1.

## Context

ADR-052's mapping layer costs a function call and an allocation per row. For
aggregates that is irrelevant. For market data it is fatal: S10 ingests on the
order of 3,000 ticks per second and S11 aggregates millions of bars. Mapping
every tick through a domain object would make ingestion 10–50× more expensive
for no benefit — a tick has no identity, no lifecycle, and no invariant beyond
its column constraints.

An unbounded exception, however, becomes a second persistence path that gets used
opportunistically wherever the mapping layer feels inconvenient, and the
architecture erodes within a year.

## Decision

Bulk timeseries writes use SQLAlchemy Core or `COPY`, bypassing the mapping
layer — and **terminate at a dedicated `TimeSeriesStorage` interface**. They do
not become a general-purpose second persistence path.

The exception applies **only** to data that is all of the following:

* append-only;
* immutable once written;
* not an aggregate;
* carrying no business invariant beyond column constraints;
* participating in no transactional decision-making;
* raising no domain events;
* having no repository semantics — no `get`, no `update`, no identity lookup.

Anything with an identity, a lifecycle, or an invariant goes through the mapping
layer, **whatever its volume**.

**Enforcement.** `TimeSeriesStorage` implementations live in a dedicated package,
and boundary rule **R8** fails the build if that package imports from any
`domain` package. A business entity therefore cannot even be *named* on this
path, let alone persisted through it.

Widening the exception requires a new ADR. This is stated so that a future
widening is a visible decision rather than an accumulated precedent.

## Rationale

Amendment 1 at Design Review tightened the boundary from a described convention
to an enforced interface, and the reasoning is sound: a convention that says
"only use this for timeseries" is obeyed until the first inconvenient deadline. A
package that cannot import a domain object is obeyed always.

Seven conditions rather than one is deliberate. "It's timeseries" is a judgement
call; "it raises no domain events and has no repository semantics" is checkable
by reading.

## Consequences

Two persistence paths exist, with a hard architectural boundary between them and
a build rule enforcing it.

`TimeSeriesStorage` is defined here and implemented by S11. Its interface accepts
column-shaped rows, not objects — there is no type on that path that could carry
domain meaning.

Reading timeseries data for analytics goes through read models, not repositories.
That is a separate concern belonging to S11.
