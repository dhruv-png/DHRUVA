# ADR-082 — Historical evaluation data must be PIT, survivorship-aware, and return-basis explicit

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

DHRUVA can replay daily bars for today's owner-selected equities, but that does
not answer which securities belonged to an evaluation universe at an old
cutoff. It excludes removed and delisted securities by construction and can
therefore create survivorship and selection bias. Provider symbols and tokens
also change while the economic instrument remains the same. Finally, a return
computed from bars whose adjustment semantics are unknown can cross a split,
bonus, demerger, dividend, or other capital action and still look numerically
plausible. More years of the same current selection do not solve these defects.

## Decision

Every historical evaluation names a first-class universe and resolves its
membership using both effective time and source-observed knowledge time. Stable
internal instrument identity survives symbol and provider-token revisions.
Historical universe and corporate-action source facts are append-only,
revisioned, attributable, idempotent, and included in deterministic evidence
provenance. Strict PIT evaluation refuses a membership, mapping, market,
benchmark, or action revision that was not known at its historical cutoff and
never falls back to the current owner watchlist.

`SURVIVORSHIP_SAFE` requires historical membership, removals, delistings,
PIT-known-at evidence, instrument lifecycle coverage, and confirmed data rights;
the presence of membership rows alone is insufficient. Security returns state
one of `RAW_PRICE`, `PRICE_ADJUSTED`, `TOTAL_RETURN`, or `UNKNOWN`; benchmark
returns independently state `PRICE_INDEX` or `TOTAL_RETURN_INDEX`. DHRUVA never
creates an adjustment factor or total return without source evidence. Suspicious
discontinuities under unknown semantics block the affected outcome rather than
being silently repaired. Provider retention, automation, and household-use
rights are evaluation-readiness dependencies requiring owner approval.

## Rationale

A single current-symbol table was rejected because symbols are mutable and do
not preserve removed securities. Reconstructing constituents from today's
instruments or press-release scraping was rejected because it fabricates
completeness and publication time. Treating any long price series as adjusted
was rejected because a wrong return can pass every numerical check. A graph
database was rejected as unnecessary: existing revision identifiers and
fingerprints provide the required dependency graph. Narrow provider ports and
offline licensed imports keep future source choice replaceable without hiding
capability gaps behind a giant generic adapter.

## Consequences

Current owner-watchlist evidence remains `DIAGNOSTIC_ONLY` regardless of metric
quality or session count. Credible evaluation now has more explicit blockers and
may legitimately produce no strict-PIT rows until licensed history exists.
Future datasets must preserve publication/known-at timestamps, removals,
delistings, mappings, corporate actions, revisions, and permitted retention.
Storage and ingestion are more involved, but a source can be added without
rewriting candidate-v0 or the evaluation arithmetic. Candidate-v0 weights and
attention separation remain unchanged.
