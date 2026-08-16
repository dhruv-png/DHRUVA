# ADR-084 — Public exchange reconstruction is manual-drop, event-time, and diagnostic-only

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

ADR-082 defines institutional historical-integrity gates and ADR-083 defines a
licensed offline import transaction. The owner has chosen not to purchase an
institutional dataset now. Official NSE reports can provide useful raw market,
security, action and benchmark evidence, but public archive depth, revisions,
historical publication time, complete delistings/actions, automated retrieval
permission and redistribution rights are not uniformly established. Treating a
later download as contemporaneous PIT would create evidence that never existed.

## Decision

DHRUVA accepts supported official public exchange files through a manual-drop,
network-free mapper only. It implements no exchange downloader while automation
terms or controls remain uncertain. Every source is immutable and hash-addressed;
unknown schemas, competing corrections and ambiguous identities fail closed.

Public canonical datasets carry the explicit `PUBLIC_RECONSTRUCTED` evidence
class, `RETRIEVED_LATER` known-at semantics and `RAW_PRICE` return basis. Event
dates may reconstruct historical event time, but never prospective archive time.
Public scientific gaps remain readiness/bias blockers even when clean bytes are
eligible for an explicitly acknowledged local research import.

The frozen `PUBLIC_LIQUID_NSE_UNIVERSE_V0` uses only trailing series,
participation, median rupee turnover and price evidence observable by cutoff T.
ISIN/security id preserve identity; ambiguous symbol continuity does not merge.
No trade/disappearance does not prove delisting. Raw bars are never adjusted;
exact action ratios and TRI exist only when source-reported. Readiness v3 names
public reconstruction separately from institutional evaluation readiness.

## Rationale

Manual drop preserves usefulness without guessing at automated access rights or
building brittle anti-bot behaviour. An explicit evidence class prevents public
files from inheriting licensed-feed claims. Later retrieval supports honest
retrospective diagnostics, while strict bitemporal labels prevent look-ahead
from being hidden. A frozen liquidity universe reduces today's-watchlist bias
without fabricating unavailable historical index constituents. Keeping raw and
derived return identities separate preserves reproducibility.

## Consequences

The owner can reconstruct multi-year NSE diagnostics at low acquisition cost,
retain inactive observations and run the existing evaluation pipeline. Data
collection remains manual and completeness work is substantial. BSE has no
mapper until an official stable schema and terms or an owner-retained sample
establish one. The result is not institutional PIT, survivorship-safe, total
return complete, licensed for redistribution or sufficient for real-money
confidence. A future licensed/prospective archive remains the promotion path.
