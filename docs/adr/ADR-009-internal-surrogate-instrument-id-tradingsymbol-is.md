# ADR-009 — Internal surrogate `instrument_id`; `tradingsymbol` is never a key

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Trading symbols change on corporate actions, F&O symbols encode their expiry, and broker instrument tokens are not stable across contract cycles. Something has to serve as identity, and the obvious candidates are all unstable.

## Decision

A stable internal UUID identifies each instrument. Broker tokens and trading symbols are *attributes with validity windows*, not identities.

## Rationale

Symbols change on corporate actions; Kite instrument tokens are not stable across expiries; F&O symbols encode expiry.

## Consequences

Instrument Master (S07) is a genuine subsystem with history, not a lookup table.
