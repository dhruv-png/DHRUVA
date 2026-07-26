# ADR-027 — Every major architectural decision is recorded as an ADR; approved architecture is never silently modified

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Introduced by Amendment A5 at approval.

## Context

The Product Owner approved the Master Project Plan on the express condition that approved architecture is never silently modified (Amendment A5). A decision log whose entries can be edited after the fact records nothing trustworthy.

## Decision

A decision is "major" — and therefore requires an ADR — if it satisfies any of: it constrains more than one bounded context; it is costly to reverse; it selects between viable alternatives; it introduces or removes a dependency on an external system; or it changes a data model, an API contract, or a security control.
ADRs live in `docs/adr/ADR-nnn-<slug>.md`, are numbered monotonically and never renumbered, and carry a status of `Proposed` · `Accepted` · `Superseded by ADR-nnn` · `Deprecated`. **An accepted ADR is immutable except for its status line.** Changing a decision means writing a new ADR that supersedes it and states what changed and why; the original stays in the repository as history.
An **Architecture Revision (AR-nnn)** is the heavier instrument, reserved for changes that invalidate a *gate* or a *scope parameter* (§1.2). It requires written Product Owner approval and a reissue of this plan at the next version.

## Rationale

The value of a decision log is destroyed the moment entries can be edited after the fact. Immutability is what makes it trustworthy six months later, when the reasoning has been forgotten.

## Consequences

CI enforces this mechanically: a check fails the build if a file in `docs/adr/` with status `Accepted` is modified in any line other than its status line, and fails if a PR touching a context's public API or a migration does not also add or reference an ADR. Enforcement ships in S01.
