# ADR-020 — Security by default: encrypted broker credentials, no secrets in the database in plaintext

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

The platform holds credentials that can move money, and from Stage 2 will hold credentials that can place orders. Credential handling is decided once and is very difficult to retrofit.

## Decision

Kite API secret and access tokens are envelope-encrypted (per-record data key, master key from environment/KMS). Tokens never appear in logs; a log redaction filter is applied globally and tested.

## Rationale

This system holds credentials that can move real money.

## Consequences

A Secrets/Vault module (S06) precedes any broker integration.
