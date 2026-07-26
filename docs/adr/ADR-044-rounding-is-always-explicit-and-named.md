# ADR-044 — Rounding is always explicit and named after the rule it implements

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

Indian transaction charges each round differently. Securities Transaction Tax
rounds to the nearest rupee; brokerage and exchange fees round to paise; stamp
duty rounds to the rupee under most state schedules. A single default rounding
mode would be wrong for most of them, and a reviewer checking a calculation
against a circular needs to see *which rule* was applied, not which mathematical
mode.

## Decision

No operation anywhere rounds implicitly. Every operation that can lose precision
takes a `RoundingPolicy`, chosen at the call site.

Policies are named after the market rule: `STT_NEAREST_RUPEE`,
`CHARGE_TO_PAISE`, `GST_TO_PAISE`, `STAMP_DUTY_NEAREST_RUPEE`. A policy carries a
mode and a quantum, so "round to the nearest rupee" is expressed as data rather
than as a special case.

Two policies exist for reasons beyond the statutory rules:

* `CONSERVATIVE_TO_TRADER` rounds away from zero, for **pre-trade estimates
  only**. An estimated cost is never understated and an estimated proceed never
  overstated.
* `EXACT` refuses to round, raising instead. Bare operators use it, which is how
  `Price × Quantity` stays expressible under ADR-043 without rounding silently.

All rounding is performed in exact integer arithmetic. Nothing converts to
`float`, and nothing depends on decimal context.

## Rationale

`CONSERVATIVE_TO_TRADER` deserves the explicit justification. Optimistic cost
estimates are one of the quieter ways a strategy appears profitable and is not:
a few basis points of understated cost, applied across thousands of simulated
trades, is the difference between a viable edge and a fictional one. Settlement
uses the exchange's actual rule; the number shown *before* the trade rounds
against us.

`EXACT` resolves what would otherwise be a conflict between this decision and
ADR-043. ADR-043 wants natural operators; this decision forbids silent rounding.
An operator that raises with a message naming the explicit-policy method
satisfies both.

## Consequences

Call sites are more verbose. `price.notional(quantity, CHARGE_TO_PAISE)` says
more than `price * quantity`, and the extra word is the rule being applied.

Every rounding policy is tested at its midpoint, in both signs. Sign handling is
where rounding implementations go wrong.
