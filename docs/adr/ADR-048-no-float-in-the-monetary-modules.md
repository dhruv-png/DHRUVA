# ADR-048 — Boundary rule R6: no float in the monetary modules

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.5, section 4. Proposed in the S03 design document and approved at Design Review.

## Context

ADR-042 removes `float` from the monetary representation. Nothing prevents
someone reintroducing it later: a rate written as `0.18`, a `float()` conversion
at an ingestion boundary, a helper annotated `-> float`. Each is one line, each
looks harmless, and each reintroduces exactly the imprecision the design removes.

The other five boundary rules exist because "we agreed not to" is not a control.
This is the same argument applied to a numeric type.

## Decision

Boundary rule **R6**: no module under `dhruva.shared.money` may contain a float
literal, a `float()` conversion, or a `float` annotation. Enforced by the AST
checker on every commit.

`isinstance(x, float)` is deliberately **permitted**. That is a guard *rejecting*
a float — the opposite of the problem. Banning it would force the money
constructors to drop the very check that stops a float reaching them at runtime.

The philosophy extends beyond the enforced scope: pricing, valuation, accounting
and risk calculations should avoid `float` unless a future ADR justifies it.
Statistical analytics — implied volatility, greeks, correlation — legitimately use
floats, because they estimate rather than settle. The rule is enforced where money
is *counted*, not where it is *modelled*.

## Rationale

Scoping the enforcement narrowly rather than platform-wide is deliberate. A rule
that fires on legitimate analytics code would be suppressed within a month, and a
suppressed rule protects nothing. Enforcing it precisely where a violation is
always wrong keeps it credible.

Distinguishing the three float *shapes* rather than banning the identifier keeps
the rule honest: the checker reports what it found — literal, conversion or
annotation — so the fix is obvious from the message.

## Consequences

Rates entering the monetary layer must arrive as `Decimal` or as a string. An
ingestion adapter parsing a broker's JSON float must convert at the boundary, and
the conversion is visible there rather than buried.

Extending R6 to further packages is a configuration change plus an ADR. S12
(Trading Cost Engine) and S24 (Portfolio Engine) are the expected candidates.
