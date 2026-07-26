# ADR-031 — Process configuration lives in the shared kernel; the Platform context owns only persisted configuration

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Raised in the S02 design document, approved as an architectural clarification of section 5 -- not a redesign of it.

## Context

Plan section 5 assigns "config" to the Platform context (C9). The S02 design
surfaced an ambiguity in that assignment: the word covers two unrelated things.

*Process configuration* — database URL, log level, environment, exporter
endpoint — is read once at startup, is identical for every user, and is needed by
every layer of every context, including `domain`.

*Persisted configuration* — feature flags, user preferences, risk limits — is
account-scoped, stored in the database, changes at runtime, and is genuinely a
Platform concern.

Routing the first kind through C9 would force nearly every context to declare a
dependency on C9. The dependency matrix would become almost fully connected,
which would drain it of the meaning that makes rule R2 worth enforcing at all.

## Decision

Process configuration lives in `dhruva.shared.config`. The Platform context (C9)
owns persisted, account-scoped configuration, and only that.

`dhruva.shared` remains a leaf: it imports nothing from `dhruva.contexts`, which
is already enforced by boundary rule R4.

The settings *schema* is defined in the shared kernel; the settings *object* is
constructed exactly once, at a composition root, and typed slices of it are
injected into the components that need them. A component that receives the whole
`Settings` object is a component that knows too much.

This is recorded as a **clarification** of plan section 5, not a revision of it.
The section's intent — keep cross-cutting concerns from coupling every context —
is served better by this reading than by the literal one.

## Rationale

The alternative, a `platform.api` export of a settings accessor, was rejected for
three reasons. It would make C9 a universal dependency, defeating R2. It would put
an import cycle risk in the kernel, since C9 itself needs logging and errors. And
it would tempt every module towards an ambient global accessor, which is the
pattern ADR-031's companion rule R5 exists to prevent.

Placing the schema in the kernel and the construction at the composition root
keeps the two properties that matter: configuration is typed and validated in one
place, and no module can reach the environment on its own.

## Consequences

Contexts receive `LogSettings` or `DatabaseSettings`, never `Settings`. This is
slightly more wiring at each composition root, and the cost is accepted.

Boundary rule R5 becomes necessary rather than optional: without a mechanical
prohibition on reading `os.environ` outside `shared.config`, this decision decays
into a convention within a few subsystems.

Plan section 5's C9 row is annotated to distinguish the two kinds of
configuration, so the ambiguity is not relitigated at S06.
