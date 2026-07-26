# ADR-012 — The Risk Engine is a mandatory, unbypassable pre-trade gate

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Every order path is a potential route to unintended capital loss. The conventional control is a documented rule that developers remember to call the risk check before placing an order.

## Decision

No code path may reach `BrokerGateway.place_order()` except through `RiskEngine.authorise()`. Enforced structurally: the gateway's order method is private to the OMS module, and the OMS constructs orders only from a `RiskApprovedOrder` token that only the Risk Engine can mint.

## Rationale

"Remember to check risk" is not a control. Make the unsafe path unrepresentable.

## Consequences

Risk Engine (S25) ships before the OMS (S33).
