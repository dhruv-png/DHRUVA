# ADR-080 - Technical candidate v0 is an experimental frozen baseline

- **Status:** Accepted
- **Date:** 2026-08-14
- **Deciders:** Product Owner (Dhruv), CTO / Principal Architect
- **Supersedes:** -
- **Superseded by:** -

> Origin: approved P0 deterministic technical research-candidate rank v0 slice.

## Context

DHRUVA's existing attention score is sign-neutral and answers what changed
enough to inspect. It is not an attractiveness rank. The next critical-path
artifact must prioritize the current owner watchlist for research using only
PIT market facts, while remaining an unevaluated baseline that later models
must beat. Current NIFTY history is a price index, Zerodha adjustment evidence
is `UNKNOWN`, and the current watchlist is not a survivorship-safe historical
universe.

The attention observation schema cannot honestly store this result: its header
requires attention, digest, news, packet, and source-health provenance, and its
members require attention bands and market/news fingerprints. Candidate facts
need different revisions, eligibility states, score bounds, and feature
availability.

## Decision

`technical-features-v0` is owned by analytics. It computes 5/20/60/120-session
simple close returns; 20/60/120-session simple excess returns against aligned
NIFTY 50 `PRICE_INDEX` sessions; close/MA50 and MA50/MA200 relationships;
population realized volatility over 20 and 60 returns annualized by square-root
252; 60-session downside semideviation using zero-target negative returns with
the same annualizer; close drawdown from the trailing 126-session close high;
ATR(14), including gaps against the previous close, divided by current close;
current volume divided by trailing-20 median volume; trailing-20 median close
times volume turnover; and a transparent NIFTY regime.

Warm-ups are exact: return and volatility window `N` require `N+1` bars; MA50,
MA200 relationship, drawdown126, ATR14, and volume/turnover20 require 50, 200,
126, 15, and 20 bars respectively. Missing values retain explicit status and
are never zero-filled. Cross-sectional calculations use only eligible members
at the supplied cutoff and deterministic midrank percentiles.

`technical-candidate-v0` is owned by intelligence. Normal eligibility requires
120-session return, 60/120-session benchmark excess return, realized volatility
60, drawdown126, volume ratio20, and median turnover20. Stale evidence, raw
unadjusted history, missing benchmark coverage, or absent essential facts
exclude the member. MA200 is optional. `UNKNOWN` adjustment evidence is allowed
only as visible degraded evidence because ingest already refuses unexplained
close discontinuities over 35%; it forces `LOW` evidence completeness. It does
not prove corporate-action correctness.

The frozen, non-optimized factor weights are:

| Component | Weight |
|---|---:|
| 60-session benchmark-relative momentum percentile | 15 |
| 120-session benchmark-relative momentum percentile | 20 |
| close relative to MA50 | 10 |
| MA50 relative to MA200 | 10 |
| 120-session return divided by realized volatility60 | 20 |
| volume ratio20 | 5 |
| median rupee turnover20 | 5 |
| lower realized volatility60 | 5 |
| smaller drawdown126 | 5 |
| benchmark regime compatibility | 5 |

Each continuous component maps its within-universe midrank percentile to
`[-100, 100]`. Regime is fixed at `100`, `0`, or `-100` for risk-on, mixed, or
risk-off. Missing optional components are excluded and remaining weights are
renormalized; no missing value becomes zero. Scores are rounded to 0.01 and
bounded to `[-100, 100]`. Tiers are fixed score semantics, not broad-market
claims: at least 50 `STRONG_CANDIDATE`, at least 20 `CANDIDATE`, at least -20
`NEUTRAL`, otherwise `WEAK`. Percentile is explicitly within the current owner
watchlist.

Evidence completeness describes input quality, never probability. `HIGH`
requires all v0 features, fresh verified/adjusted evidence, and a TRI benchmark;
`MODERATE` permits nonessential gaps without those material limitations; `LOW`
captures essential exclusion or unknown adjustment evidence. Fundamentals and
news do not enter v0. Attention is carried only as separately named metadata
and never enters a candidate score.

Manual `dhruva-research candidates` computes from local PIT archives without a
provider call or write. `--freeze` explicitly records a weekly official result;
routine refresh does not. A dedicated append-only, account-owned
`candidate_ranking_observation` stores the canonical full JSON payload and
fingerprint. It preserves all members, exclusions, scores/ranks/tiers, evidence,
feature availability, revisions, universe, benchmark limitation, and separate
attention metadata. Identical retries are idempotent. Database triggers refuse
update, delete, and truncate.

## Rationale

Medium-term relative momentum receives the largest weight because the output is
a directional research-priority baseline; risk-adjusted momentum is next so a
volatile path cannot earn the same interpretation solely from endpoint return.
Trend confirms rather than dominates. Liquidity, participation, volatility,
drawdown, and regime have deliberately smaller roles that can penalize fragile
evidence without overwhelming direction. Fixed score bands avoid pretending a
rank among roughly twenty owner-selected stocks is a broad-market percentile.

Keeping pure feature arithmetic in analytics makes historical cutoffs callable
without a worker or database. Keeping factor assembly in intelligence preserves
the boundary direction and leaves attention unchanged. A dedicated candidate
freeze table is narrower than making the attention schema nullable and
polymorphic; canonical JSON retains the complete versioned observation while
indexed columns retain its identity and audit axes.

## Consequences

- Alembic advances to `0019_candidate_ranking`; attention rows and semantics are
  unchanged.
- The output is always `EXPERIMENTAL RESEARCH CANDIDATE`; it is not a buy, sell,
  recommendation, expected return, probability, or AI prediction.
- Empty or shallow owner history produces explicit `DATA_UNAVAILABLE` or
  `INSUFFICIENT_HISTORY` exclusions rather than blocking implementation.
- Historical walk-forward evaluation is the next P0 slice. No weight may be
  tuned against future outcomes in this baseline slice.
- TRI, corporate-action provenance, PIT fundamentals, improved news evidence,
  and a survivorship-safe universe remain later dependencies.
