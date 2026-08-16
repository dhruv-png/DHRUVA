# Model evidence and prospective outcomes

All commands in this runbook use local PostgreSQL only. They do not contact
Zerodha, GDELT, NSE, an order endpoint, or any other provider.

## Retrospective diagnostic

Run the frozen candidate model weekly over locally persisted bars:

```powershell
Set-Location C:\Users\dhruv\Projects\DHRUVA\backend
& .venv\Scripts\dhruva-research.exe evaluate `
  --account owner-family `
  --universe current-owner-watchlist `
  --model technical-candidate-v0 `
  --from 2025-01-01 `
  --to 2026-07-01 `
  --horizons 20 60 `
  --cost-bps 20
```

The command uses the final persisted NIFTY session in each ISO week. It applies
the exact `technical-features-v0` calculator and `technical-candidate-v0`
ranker at every cutoff. Entry is the next session open and exit is the 20th or
60th holding-session close. Missing sessions are not shortened.

This is always labelled `RETROSPECTIVE DIAGNOSTIC ONLY`: it applies today's
owner watchlist to reconstructed history. That selection is not survivorship
safe, and the bars were not known to DHRUVA at their original market dates.

To audit only membership and revisions DHRUVA truly knew at each cutoff, add
`--strict-pit`. With the current reconstructed archive this will correctly yield
little or no old ranking evidence; it does not reinterpret later backfills as
facts known earlier:

```powershell
& .venv\Scripts\dhruva-research.exe evaluate `
  --account owner-family --from 2026-08-01 --to 2026-08-14 --strict-pit
```

Write deterministic machine-readable evidence when needed:

```powershell
$evidencePath = Join-Path ([System.IO.Path]::GetTempPath()) 'dhruva-model-evidence.json'
& .venv\Scripts\dhruva-research.exe evaluate `
  --account owner-family --from 2025-01-01 --to 2026-07-01 `
  --export $evidencePath
```

The `dhruva.model-evidence.v1` envelope contains a content SHA-256 and embeds
`dhruva.evaluation-data-readiness.v3`. It names the universe id/source,
membership and mapping revisions, bar/benchmark revisions, return and benchmark
bases, source status, survivorship fields, provenance gaps, and blockers.
Identical persisted state and explicit options produce identical bytes.

## Prospective evidence clock

An explicit candidate `--freeze` records the ranking before outcomes are known.
After future sessions are locally persisted, materialize outcomes:

```powershell
& .venv\Scripts\dhruva-research.exe outcomes `
  --account owner-family --cost-bps 20
```

The operation is safe and idempotent. Mature outcomes append beside the frozen
ranking; they never update it. Pending observations remain pending until the
exact benchmark horizon exists; temporal immaturity takes precedence over
member-data availability, including a weekend freeze. Missing benchmark,
mature stock sessions, or
unsupported raw adjustment semantics are reported rather than fabricated.

Inspect the clock without calculating or writing anything:

```powershell
& .venv\Scripts\dhruva-research.exe evidence --account owner-family
```

The current benchmark is NIFTY 50 `PRICE_INDEX`; dividends are excluded.
Zerodha adjustment semantics are `UNKNOWN`. These outputs are paper research,
not advice, BUY/SELL instructions, or evidence of profitability.

## Readiness gates

Feature readiness at 200/252 sessions is not evaluation readiness. Serious
`EVALUATION_READY` evidence requires a historical PIT/survivorship-safe universe,
verified return-basis/corporate-action semantics, PIT known-at, removals,
delistings, lifecycle mappings, source rights, no critical provenance gaps, at
least 2,000 sessions, at least 104 weekly ranking periods, and at least 100
mature member outcomes at both 20 and 60 sessions. Until those gates are met the result is
`DIAGNOSTIC_ONLY`, `INSUFFICIENT_DATA`, `DEGRADED`, or `UNSUPPORTED`.

Inspect a future ten-year acquisition plan without contacting a provider:

```powershell
& .venv\Scripts\dhruva-marketdata.exe backfill-plan `
  --account owner-family --years 10 --as-of 2026-08-16T00:00:00Z
```

This only plans the existing deterministic request chunks. Ten years of today's
watchlist would still be selection-biased; credible evaluation also needs a
licensed PIT universe with removals/delistings and auditable corporate actions.
Use `dhruva-reference universe-readiness` and
`docs/runbooks/historical-evaluation-data.md` to inspect those gates.
