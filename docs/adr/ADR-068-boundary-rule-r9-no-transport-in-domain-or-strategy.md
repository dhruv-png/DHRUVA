# ADR-068 — Boundary rule R9: no strategy or domain module imports a transport

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S05 design document `docs/design/S05-event-bus-and-job-runtime.md`, approved at Design Review. Refines ADR-002, which fixed the envelope and the transport in Phase 0; nothing here replaces it.

## Context

ADR-067 makes replay possible by putting a port between consumers and Redis. A
port only helps if nothing bypasses it, and importing a client library is always
the convenient one-liner.

A strategy that imports `redis` or `celery` cannot run inside a backtest process.
The failure is not a crash at import; it is a strategy that silently cannot be
tested against history.

## Decision

**Rule R9.** The build fails if any module under a `domain/` or `strategy/`
package imports `redis`, `celery`, `kombu`, or any `infrastructure.messaging`
module.

Enforced by the existing AST boundary checker, alongside R1-R8.

## Rationale

R6, R7 and R8 established the pattern: where a decision matters and is easy to
violate in one convenient line, it becomes a build rule rather than a convention.
This qualifies more than any of them, because the cost of a violation is not a
broken build but a strategy nobody can backtest -- discovered months later, in
the subsystem that most needs the evidence.

Rules R7 and R8 protect persistence boundaries. R9 does the same for transport,
and the three together are what make ADR-010's one-kernel promise structural
rather than aspirational.

## Consequences

Consumers receive their `EventStream` by injection at a composition root. There
is no way to reach a transport from strategy code, by design.

Test doubles must be ports too. A test that reaches for a real Redis client
inside strategy code fails the build, which is the correct outcome.

`strategy/` does not exist yet. The rule is written now so that it is already
enforced on the first module placed there, rather than being retrofitted against
existing violations.
