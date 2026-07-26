# ADR-006 — All timestamps stored UTC; all display Asia/Kolkata; `TradingDay` is a domain type

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

The platform runs in a single timezone but stores data whose meaning depends on session boundaries rather than calendar days. 'Today' is ambiguous across midnight, across non-trading days, and across the ingest/event distinction.

## Decision

`TIMESTAMPTZ` everywhere, UTC in storage. A `TradingDay` value object is distinct from a calendar date and is resolved through the Calendar Engine.

## Rationale

"Today" is ambiguous across midnight, DST-free-but-offset IST, and non-trading days. Most Indian market bugs are timezone bugs.

## Consequences

Naive `datetime` is banned; enforced by a custom lint rule.
