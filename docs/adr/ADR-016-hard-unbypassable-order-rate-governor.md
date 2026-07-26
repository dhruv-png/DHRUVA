# ADR-016 — Hard, unbypassable order-rate governor

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Kite caps order throughput, and SEBI's framework classifies systems exceeding ten orders per second per market segment as registered algos requiring exchange approval (plan section 2.2). Both are hard external limits with real consequences.

## Decision

A token-bucket governor in the OMS, default 2 orders/sec, configurable **downward only** at runtime; raising it requires a code change and an Architecture Revision.

## Rationale

Both a broker limit and the SEBI algo classification threshold (§2.2).

## Consequences

Recorded as a compliance control, tested explicitly, and included in the audit log.
