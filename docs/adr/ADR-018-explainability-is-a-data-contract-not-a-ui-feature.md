# ADR-018 — Explainability is a data contract, not a UI feature

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Explainability is a stated differentiator (plan section 1.1). It can be built as a rendering layer over model outputs, or as a constraint on which models are admissible in the first place.

## Decision

Every signal emits a structured `EvidenceBundle` (contributing factors, weights, regime context, opposing evidence, confidence interval, data provenance). A signal that cannot produce one is rejected at the domain boundary.

## Rationale

Explanations bolted on after the fact are rationalisations. This also makes low-quality models visibly low-quality.

## Consequences

Constrains model selection — unexplainable models are only usable as one bounded input to an explainable consensus layer, never as the sole decision maker.
