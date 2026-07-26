# ADR-002 — Kafka deferred; Redis Streams is the v1 event backbone

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

An event backbone is needed from S05 onward. The stack list permits Kafka 'where justified'. At single-user volume that justification has to be established rather than assumed, but the choice determines the shape of every cross-context interaction.

## Decision

Event envelope is transport-agnostic (`event_id`, `event_type`, `event_version`, `occurred_at`, `recorded_at`, `account_id`, `correlation_id`, `causation_id`, `payload`). Redis Streams + consumer groups implement it in v1.

## Rationale

Project brief permits Kafka "where justified." At single-user volume it is not justified; it is one more thing to operate.

## Consequences

Migration to Kafka is an adapter swap. No business code changes.
