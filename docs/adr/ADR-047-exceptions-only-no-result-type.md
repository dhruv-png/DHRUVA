# ADR-047 — Exceptions are the single error-signalling mechanism; no Result type

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

The Master Project Plan lists a `Result` type in S03's scope. Result types are
genuinely good: they make failure part of a function's signature, and they compose.

They are also a second error-signalling idiom in a codebase that already has one.
ADR-038 established an exception taxonomy with stable codes, structured context
and a safety branch. Adding `Result` alongside it creates a boundary question —
which does this function use? — that would be relitigated in every review for the
next five years.

## Decision

Exceptions are the only error-signalling mechanism. No `Result`, no `Either`, no
error-return convention.

`Result` is removed from S03's scope.

## Rationale

Consistency across a codebase one person maintains for a decade is worth more
than the theoretical advantages of either idiom in isolation. The failure mode of
mixed idioms is not that one is wrong; it is that the boundary between them
becomes a matter of taste, and taste drifts.

Python's ecosystem also pushes hard toward exceptions. Every library this
platform depends on raises. A `Result`-based core would spend its life converting
at the boundaries, which is overhead with no correctness benefit.

The argument *for* `Result` — that failure should be visible in a signature — is
partly satisfied by ADR-038 already: every raised error carries a typed code, and
docstrings state what raises. That is weaker than a type-system guarantee, and
accepting the weaker form is the trade being made deliberately.

## Consequences

Callers must read docstrings to know what raises. `Raises` sections become part of
the Definition of Done rather than a nicety.

Where accumulating multiple validation failures is genuinely needed — form
validation, batch import — a dedicated report object may be introduced for that
purpose. That is not a `Result` and must not become one; it accumulates facts
rather than short-circuiting control flow.
