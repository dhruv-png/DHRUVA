# ADR-050 — The shared kernel is API-stable from S03

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Introduced by Amendment A16 at the conclusion of S03.

## Context

Forty-three subsystems remain, and every one of them will import from the shared
kernel. `Money` will appear in the cost engine, the portfolio engine, the risk
engine, the backtester and the reporting layer. `TradingDay` will key most of the
market-data schema.

A change to any of these is not a local edit. It is a change to the vocabulary
the whole platform speaks, and by S20 the cost of one will be measured in days of
mechanical churn plus the risk that some call site was updated incorrectly.

The types are also, as of the end of S03, *demonstrably* sound: 864 tests, 98.6%
coverage, property-based verification of the algebraic laws, and a mutation
score establishing that the tests assert rather than merely execute. This is the
right moment to stop moving them.

## Decision

The following are **API-stable** from the conclusion of S03:

`Money` · `Price` · `Quantity` · `SignedQuantity` · `Side` · `Ratio` ·
`RoundingPolicy` · `Currency` · `TradingDay` · `TradingSession` ·
`TradingCalendar` · `Clock` · `DateRange` · `TimeRange` · `InstrumentId` ·
`AccountId` · `SurrogateId` · `DomainEvent`

Any change to their public surface — a method signature, a constructor, a
semantic, a removal — requires a **dedicated ADR and explicit Product Owner
approval before implementation**.

Explicitly **not** covered, because these are not the public surface:

* Adding a new named `RoundingPolicy`. Policies are data; the type is stable.
* Adding a new `Currency` member.
* Internal optimisation that preserves observable behaviour — the hot-path guard
  rewrite during S03 is the model: measured, behaviour-preserving, tested.
* Adding a new `DomainEvent` **subclass**. The base is stable; facts accumulate.
* Adding a new error to the taxonomy within an existing family.

## Rationale

Stability is declared *now* rather than later because the cost of change grows
with the number of call sites, and the number of call sites grows monotonically
from here. Declaring it at S30 would be declaring it after the expensive period.

The carve-outs matter as much as the freeze. A stability rule that also forbade
adding a rounding policy or a new event type would be obstructive rather than
protective, and would be routed around within a quarter. What is frozen is the
*shape*; what stays open is the *content*.

The alternative — semantic versioning of the kernel with deprecation cycles — was
considered and rejected as disproportionate. There is one consumer repository and
one maintainer. A deprecation cycle protects downstream consumers who cannot
change in step; here they can, and the ADR requirement provides the deliberation
that a deprecation cycle would otherwise supply.

## Consequences

Design work moves earlier. A subsystem needing something the kernel does not
offer must raise it at its own Step 1, not discover it mid-implementation and
patch the kernel.

Some subsystems will work around a kernel limitation rather than change the
kernel. That is the intended trade, and where a workaround appears twice it
becomes evidence for the ADR that changes the kernel properly.

`docs/DOMAIN.md` becomes the normative description of these types. It is updated
in the same commit as any approved change.
