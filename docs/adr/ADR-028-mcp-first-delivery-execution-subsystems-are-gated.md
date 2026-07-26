# ADR-028 — MCP-first delivery; execution subsystems are gated on demonstrated analytics value

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Introduced by Amendment A4 at approval.

## Context

The full platform is roughly 316 sessions of work. Its analytics half and execution half are separable, and the second has no value if the first has no edge (risks R01 and R05). The Product Owner approved MCP-first delivery (Amendment A4).

## Decision

Stage 1 (S01–S20 plus a thin read-only dashboard) is the approved delivery target. Subsystems S21–S40, and in particular the entire execution path (S23–S34), are **not authorised** until Gate **G-MCP** passes. No broker credential with order permissions is provisioned in any environment during Stage 1, and no code that can construct an order exists in the repository during Stage 1.

## Rationale

The execution half of this platform (~120 sessions) has zero value if the analytics half has no edge. Testing the thesis at Week 28 rather than Week 50 is the single highest-leverage risk reduction available (R01, R05).

## Consequences

The Trading (C7) and Risk (C6) contexts exist in Stage 1 **only as empty package boundaries with import-linter contracts**, so that the layout is settled and cannot drift — but they contain no logic. Gate G-MCP has an explicit, pre-agreed "the thesis did not hold" branch (§8, G-MCP), which is a legitimate and expected outcome, not a failure.

---
