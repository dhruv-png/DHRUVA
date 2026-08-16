# ADR-081 - Candidate evaluation is versioned, PIT-bounded, and append-only

- **Status:** Accepted
- **Date:** 2026-08-16
- **Deciders:** Product Owner (Dhruv), CTO / Principal Architect
- **Supersedes:** -
- **Superseded by:** -

> Origin: approved autonomous evidence and evaluation milestone for the frozen
> `technical-candidate-v0` baseline.

## Context

DHRUVA can freeze a deterministic candidate ranking, but a ranking is not
evidence that its ordering has predictive information. Evaluation needs to join
facts observable at a ranking cutoff to later outcomes without changing the
ranking, leaking future bars into features, assuming same-close execution, or
presenting today's owner-selected watchlist as a survivorship-safe historical
universe.

The current two-year archive was reconstructed after its market dates. Zerodha
adjustment semantics remain `UNKNOWN`, and NIFTY 50 is a `PRICE_INDEX`, not TRI.
Those facts can support a useful retrospective diagnostic but cannot establish
credible historical model efficacy.

## Decision

`technical-candidate-evaluation-v0` evaluates the unchanged
`technical-candidate-v0` ranker and `technical-features-v0` features at explicit
cutoffs. Weekly replay uses the final persisted benchmark session in each ISO
week, never an assumed calendar Friday. Full series may be supplied to the pure
feature calculator, but it structurally excludes bars after cutoff T.

The paper execution contract is: a ranking is produced after completed session
T; entry is the next aligned session's open; exit is the close of holding
session 20 or 60. A missing required session never shortens the horizon. Research
cost is an explicit non-negative round-trip basis-point deduction, defaulting to
20 bps. NIFTY uses the same entry and exit sessions and remains labelled
`PRICE_INDEX`.

Future outcomes are separate append-only `candidate_outcome` facts referencing
the original candidate observation and instrument. They record ranker, feature,
evaluation, outcome and schema revisions; entry/exit prices and dates; gross,
benchmark, excess and explicitly costed returns; MAE, MFE, holding-period
drawdown and realized volatility; adjustment and benchmark semantics; bar
revision provenance; and a deterministic content hash. PostgreSQL rejects
update, delete and truncate. Identical materialization is idempotent. A later
source correction appends a different fact rather than rewriting one.

The retrospective dataset retains every eligible or excluded ranked member and
its explicit horizon maturity state. Metrics are deterministic: per-date
Spearman rank IC and aggregate distribution, top-1/3/5 equal-weight cohorts,
rank buckets, candidate tiers, benchmark, equal-weight eligible universe and a
120-session relative-momentum baseline. No confidence interval is reported until
an approved dependence-aware method and adequate sample exist.

Readiness is separate from feature availability. `EVALUATION_READY` requires a
survivorship-safe historical PIT universe, verified adjustment semantics, at
least 2,000 sessions, at least 104 ranking periods and at least 100 mature rows
at each primary horizon. A current-watchlist retrospective replay is always
`DIAGNOSTIC_ONLY`, regardless of attractive metrics. Machine output uses the
byte-deterministic `dhruva.model-evidence.v1` schema.

## Rationale

Next-session open is the earliest defensible execution price in daily OHLCV
after a close-based signal. Defining maturity through benchmark sessions makes
holidays explicit and exposes a missing stock bar instead of silently changing
the holding period. Separate outcome rows preserve the prospective evidence
clock and keep the answer key out of the frozen ranking fingerprint.

Reusing the production feature calculator and ranker avoids a backtest-only
model that can drift. Explicitly naming the current-watchlist diagnostic makes
the useful calculation available now without laundering selection bias into a
validation claim.

## Consequences

- Alembic advances to `0020_candidate_outcome` with RLS scaffolding and database
  append-only guards.
- `dhruva-research evaluate`, `outcomes`, and `evidence` remain local and make no
  provider or order call.
- `UNKNOWN` adjustment permits diagnostic arithmetic only and remains a visible
  limitation; `RAW` fails closed.
- Total-return claims, historical constituent reconstruction, statistical
  significance and model-weight tuning remain unsupported.
- Serious validation still needs roughly 8-10 years of licensed history, a PIT
  universe including removals/delistings, and auditable corporate actions/TRI.
