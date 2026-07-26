# ADR-021 — Daily "Market Open Ritual" is a designed feature

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Kite access tokens expire daily and require an interactive login with TOTP (plan section 2.1). Unattended operation is therefore impossible rather than merely inconvenient, and the constraint can be fought or modelled.

## Decision

Kite's daily token expiry is modelled explicitly as a session lifecycle: a pre-open human re-auth flow, followed by automated readiness checks (calendar, instrument master refresh, reconciliation, subscription warm-up, data-freshness verification).

## Rationale

§2.1 — unattended operation is impossible. Model the constraint rather than fighting it.

## Consequences

Session state is a first-class domain concept. The system knows whether it is authorised, degraded, or offline, and the UI always shows which.
