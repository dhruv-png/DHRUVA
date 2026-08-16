# ADR-083 — Offline historical imports require a licensed manifest and row provenance

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

ADR-082 defines what credible PIT evaluation needs but did not define an
executable acquisition boundary. A licensed delivery spans Reference identities,
universe membership/actions, and Market Data bars. Applying those contexts in
separate transactions can leave a plausible but incomplete archive. Trusting a
filename or provider name cannot prove retained bytes, schema, licensing,
publication time, revision lineage, or which source row created a fact.

## Decision

DHRUVA accepts historical data only as an offline immutable directory governed
by `dhruva.historical-dataset-manifest.v1`. Preflight is network- and
database-free, verifies every payload hash/count/exact schema, validates
cross-file semantics, and emits a deterministic disposition. Only `READY` may
be applied. Authoritative apply additionally requires an owner-reviewed licence
state explicitly confirming local retention or automated analysis.

The `dhruva.ingest` composition root owns one transaction across existing
Reference and Market Data repositories. It appends the manifest, file metadata,
canonical facts, source-row provenance, and deterministic import result
together. Identical retry is a no-op; conflicting source-revision reuse fails;
correction is a new revision. Dataset ledgers are tenant-scoped, RLS-enrolled,
and protected by append-only database triggers. Candidate-v0 and attention
semantics do not change.

## Rationale

Direct provider adapters were rejected because no provider is approved and
transport/authentication would mix acquisition rights with canonical integrity.
A generic staging table was rejected because opaque rows defer semantic failure
until after persistence. Separate context transactions were rejected because
partial success corrupts readiness. Inferring a licence from provider identity
or public access was rejected because only the retained agreement can grant
use/retention rights. In-place correction was rejected because it destroys the
PIT record and makes prior evidence irreproducible.

## Consequences

Owner-supplied CSV deliveries can now be safely inspected, quarantined, and
replayed without provider access. Every imported fact has exact file/hash/row,
mapping, known-at, and revision provenance. Delivery preparation is stricter,
and Parquet needs a future versioned schema rather than silently sharing CSV
semantics. BSE apply remains blocked until the NSE-only personal-MVP identity
invariant is deliberately expanded. No public-source research or successful
synthetic preflight makes a real provider approved.
