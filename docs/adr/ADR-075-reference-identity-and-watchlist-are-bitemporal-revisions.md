# ADR-075 — Reference identity and watchlist membership are bitemporal revisions

- **Status:** Accepted
- **Date:** 2026-08-02
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Personal MVP Plan v1.0, MVP 1. The Product Owner supplied and approved
> the exact shared universe and its dynamic futures-eligibility rule in writing.

## Context

The two users share one owner-maintained cash-equity watchlist. Symbols, company
names, ISINs and provider mappings can change; removed members must remain in
history; news needs aliases and former names; and every backtest must see only
what DHRUVA knew at its as-of instant. Zerodha instrument tokens cannot identify
the instrument because contract and provider mappings change (ADR-009).

The owner also intends every selected equity for futures research, but intent is
not evidence that an active, liquid NSE futures contract exists today. Treating
the static list as permanent eligibility would eventually fabricate a contract
or silently scan an expired one.

## Decision

The Reference context owns a stable internal `InstrumentId` and two append-only
revision histories:

1. instrument identity revisions carry the effective window, recorded-at time,
   canonical NSE symbol, exact company name, aliases, former names, nullable
   ISIN, sector, concentration groups, cash mapping and futures-research intent;
2. tenant-scoped watchlist membership revisions carry independent active and
   recorded-at windows.

Queries resolve both effective date and knowledge time. A later correction never
rewrites the earlier revision. Source plus immutable source-revision keys make a
retry idempotent; reusing one key for different content fails closed. The
account-owned membership table receives permissive v1 RLS scaffolding.

The committed owner configuration contains exactly the twenty approved equities
and a separate, non-watchlist Nifty 50 benchmark identity. It records futures
research intent only. Each future instrument-master refresh will derive current
availability from unambiguous active `NFO-FUT` contracts with positive lot size
and current tokens; no contract means `CURRENTLY_UNAVAILABLE`, not an invented
instrument or a failed cash scan.

Sector classifications remain portfolio-limit inputs. `Adani Group` is recorded
as an informational concentration group; no additional hard group cap is created
without another owner decision.

## Rationale

A mutable current-state table was rejected because it erases former symbols and
lets corrections leak into old backtests. Using a provider token or trading
symbol as identity was rejected by ADR-009. Making the watchlist provider-owned
was rejected because a provider outage must not remove the owner's universe.
Hard-coding futures eligibility was rejected because exchange listings, expiries
and lot sizes are temporal provider facts, not owner configuration.

Separate identity and membership histories cost one join but preserve the two
independent questions: “what was this instrument called?” and “was it selected?”

## Consequences

- Migration `0013_reference_watchlist` adds stable instruments, identity
  revisions and account-owned membership revisions.
- The owner file is reviewable, exact configuration—not a DHRUVA recommendation.
- `NAM-INDIA` and `M&M` punctuation is preserved as identity data.
- ISIN and provider tokens may be absent until a verified instrument-master
  refresh supplies them; absence remains visible.
- Watchlist reads require both an effective date and a known-at instant.
- Later provider-contract work stores cash and futures identities separately and
  associates company news through the stable underlying.
