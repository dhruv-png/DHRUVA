# ADR-042 — Money is an integer count of minor units; Price carries a fixed high-precision scale

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** ADR-005
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

ADR-005 specified `Money` as a value object wrapping `Decimal`, persisted as
`BIGINT` minor units. S03's architecture surfaced two problems with that.

The first is a correctness problem the roadmap did not anticipate. Paise are
sufficient for equities and options, both of which quote to two decimal places.
NSE **currency derivatives quote to four** — USD/INR ticks at ₹0.0025. A price
type fixed at paise scale would silently round every currency-derivative price,
invisibly, until somebody reconciled a position.

The second is a determinism problem. `Decimal` arithmetic reads a thread-local
context. Two processes configured with different `getcontext().prec` produce
different results from identical inputs, which is a determinism defect one layer
below ADR-011 and would be near-impossible to diagnose from a discrepancy report.

## Decision

`Money` holds an **integer count of minor units** plus a `Currency`. `Price` holds
an **integer at a fixed scale of eight decimal places**, chosen once and never
varied by instrument. `Decimal` is a parsing and formatting type only; it never
carries state.

ADR-005's still-valid clauses are restated here so nothing is lost by the
supersession:

* No monetary value is ever represented as a `float`.
* Storage remains `BIGINT` minor units plus a currency code.
* `Money` is the single monetary type; there is no second one.

Instrument metadata governs display precision and tick validation (S07). It never
changes the internal representation, so a price means the same thing everywhere.

## Rationale

Superseding rather than amending, because ADR-027 forbids editing an accepted
record and ADR-005 explicitly says "wrapping `Decimal`". A partial amendment
would leave the corpus ambiguous about which clause still holds.

Eight decimal places rather than four or six: four is the minimum that works
today, six was the initial proposal, and eight covers FX spot convention (five)
with room for exchanges the platform does not yet support. At eight places a
`BIGINT` still represents prices up to roughly ₹92 billion, five orders of
magnitude beyond anything traded.

Integers are also faster than `Decimal` by roughly an order of magnitude, which
matters because a backtest performs on the order of 10⁸ monetary operations —
but speed is the third reason here, not the first.

## Consequences

Every arithmetic operation crossing the price/money scale boundary must round,
and therefore must be given a policy (ADR-044). This is more verbose than
`price * quantity` and it is the point.

`Decimal` remains in `Ratio`, where rates are applied per trade rather than per
tick and readability is worth more than speed.

ADR-005 is marked superseded and retained in full as history.
