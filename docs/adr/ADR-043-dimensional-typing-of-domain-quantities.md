# ADR-043 — Dimensional typing: Money, Price, Quantity and Ratio are distinct types

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

Money, a price per unit, a count of units and a dimensionless rate are four
physically different quantities. Represented as bare numbers, or as one shared
numeric type, nothing prevents adding a price to a cash amount or multiplying two
prices — and the result is a plausible number that a reviewer would have to
notice by intuition.

## Decision

Four distinct types, with operations defined only where they are dimensionally
meaningful.

| Expression | Result |
|---|---|
| `Money ± Money` | `Money` (same currency enforced) |
| `Money × int` | `Money` (exact) |
| `Money ÷ int` | `Money` (rounding policy required) |
| `Money ÷ Quantity` | `Price` (rounding policy required) |
| `Money ÷ Money` | `Ratio` (dimensionless) |
| `Price × Quantity` | `Money` (rounding policy required) |
| `Price ± Price` | `Price` (a spread is still a price) |

Operations that deliberately do not exist: `Money × Money`, `Price × Price`,
`Money + Price`, `Money + int`.

Prevention is **static first**: each binary operator annotates its precise
operand type, so `Money + Price` is a type error before it is a runtime error. A
runtime `isinstance` guard returning `NotImplemented` is retained beneath it, so
an untyped caller gets a `TypeError` rather than an `AttributeError` from a
half-executed operation.

## Rationale

The static/runtime pairing is deliberate and is the part worth defending. Static
typing alone leaves untyped call sites — deserialised data, dynamic dispatch,
notebooks — unprotected. Runtime guards alone give up the compile-time feedback
that makes the constraint cheap to obey.

The cost is that mypy reports the runtime guards as unreachable, which is correct
statically and wrong dynamically. That is resolved by a single scoped
configuration exception rather than by dozens of inline suppressions.

## Consequences

`mypy` `warn_unreachable` and `redundant-expr` are disabled for
`dhruva.shared.money` and `dhruva.shared.time` only, with the reasoning recorded
in `pyproject.toml`. Every other module keeps both.

Tests asserting the refusals carry `# type: ignore[operator]` deliberately: the
ignore is evidence that the static rejection fires.
