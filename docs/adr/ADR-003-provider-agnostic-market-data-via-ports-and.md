# ADR-003 — Provider-agnostic market data via ports and adapters

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.1, section 4. Approved with the plan.

## Context

Only Zerodha Kite is implemented in v1. Kite's historical depth and snapshot tick semantics are known to be limiting (plan section 2.1), so a vendor change is plausible within this platform's life.

## Decision

`MarketDataProvider`, `HistoricalDataProvider`, `BrokerGateway`, and `InstrumentCatalogue` are domain ports. `KiteAdapter` implements them in v1. **No Kite type, field name, or enum may appear outside `infrastructure/brokers/kite/`.**

## Rationale

Vendor lock-in at the domain layer is the most expensive mistake available here.

## Consequences

A translation layer and its tests must be written even though there is only one provider. This cost is accepted deliberately.
