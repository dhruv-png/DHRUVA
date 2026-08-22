# Public exchange data reconstruction runbook

All commands below are network-free. Download through the official exchange
website manually. Do not script endpoints, bypass blocks/CAPTCHA, rotate
identities, or place raw exchange files in Git.

## 1. Create the local drop

Prefer a directory outside the repository:

```powershell
$Drop = 'C:\Users\dhruv\DHRUVA-data-drop'
$Output = 'C:\Users\dhruv\DHRUVA-public-data-output\nse-v1'
New-Item -ItemType Directory -Force `
  "$Drop\nse\bhavcopy", `
  "$Drop\nse\exchange-monthly", `
  "$Drop\nse\security-master", `
  "$Drop\nse\corporate-actions", `
  "$Drop\nse\index" | Out-Null
```

Repository-local `data-drop/` and `public-data-output/` are ignored as a
backstop. Original files are immutable evidence; do not rename their internal
ZIP member or edit corrections in place.

## 2. Review source capabilities and plan

```powershell
Set-Location 'C:\Users\dhruv\Projects\DHRUVA\backend'

& .venv\Scripts\dhruva-public.exe sources --json `
  --report "$Drop\public-capability-matrix.json"

& .venv\Scripts\dhruva-public.exe plan --source nse `
  --from 2016-01-01 --to 2026-01-01 --local-root "$Drop\nse" `
  --report "$Drop\nse-acquisition-plan.json"

& .venv\Scripts\dhruva-public.exe acquisition-options --source nse `
  --years 10 --as-of 2026-08-22 `
  --report "$Drop\nse-acquisition-options.json"
```

For April 2016 onward, the planner prefers one official **Exchange Monthly
Report** per month because its reviewed `Transaction Data` sheet contains daily
traded-equity OHLCV, ISIN, series, turnover and trade count. It schedules annual
MII security snapshots only where the file is publicly documented from
2024-02-05, plus separate actions, price-index and TRI exports. Any earlier gap
remains daily/archive work. `acquisition-options` compares this strategy with
daily, clearing-only, security-wise and unverified-annual alternatives, including
file/click counts and missing evidence. Neither command downloads anything.

Use the exact owner steps and field-by-field limitations in
[NSE bulk/monthly acquisition review](../data/nse-bulk-acquisition-review.md).
The monthly report replaces traded bars, not a complete listed-security or
lifecycle history. Disk size remains `UNKNOWN` until retained files support a
defensible estimate.

## 3. Inspect and preflight

```powershell
& .venv\Scripts\dhruva-public.exe inspect-drop --drop "$Drop\nse" `
  --report "$Drop\drop-inspection.json"

& .venv\Scripts\dhruva-public.exe preflight --drop "$Drop\nse" `
  --report "$Drop\public-preflight.json"

& .venv\Scripts\dhruva-public.exe coverage --drop "$Drop\nse" `
  --report "$Drop\coverage-scorecard.json"

& .venv\Scripts\dhruva-public.exe bias --drop "$Drop\nse" `
  --report "$Drop\bias-report.json"
```

Stop on unknown schemas or competing corrected files. Select the intended
official revision by removing the other candidate from the selected drop; do
not alter either source. Review weekday gaps against official holiday/special
session evidence. Warnings about absent actions, security snapshots, TRI,
revision history, delistings and prospective known-at are limitations, not
facts to fabricate.

## 4. Reconstruct the canonical pack

Record the actual manual retrieval instant; never substitute the market date:

```powershell
& .venv\Scripts\dhruva-public.exe reconstruct-universe `
  --drop "$Drop\nse" --output "$Output" `
  --retrieved-at 2026-08-16T12:00:00Z `
  --benchmark-basis PRICE_INDEX

& .venv\Scripts\dhruva-data.exe preflight `
  --manifest "$Output\manifest.json" `
  --report "$Output\canonical-preflight.json"
```

Choose `TOTAL_RETURN_INDEX` only when an official NIFTY 50 TRI CSV is present.
This selects a separate benchmark series; it does not adjust stocks or turn
their raw returns into total return. `build-manifest` is an equivalent named
entry point for automation that wants the whole canonical reconstruction pack.

The builder streams source rows, batches into an indexed temporary SQLite
spool, writes bars in canonical security/date order, and keeps bounded 60-session
liquidity histories. The spool is removed after completion. Original source
hashes must remain unchanged.

## 5. Review and explicitly import

Canonical `READY` means integrity/import eligibility, not institutional
readiness. Read `reconstruction-manifest.json`, `public-action-evidence.jsonl`,
`security-observations.csv`, canonical preflight blockers, coverage, and bias
first.

```powershell
& .venv\Scripts\dhruva-public.exe import --apply `
  --account owner-family --manifest "$Output\manifest.json" `
  --report "$Output\import-result.json"
```

Without `--apply`, import refuses before opening PostgreSQL. The existing
single-transaction importer retains dataset/file/row provenance and is
idempotent. A corrected archive becomes a new source/dataset fingerprint.

## 6. Evaluate explicitly

```powershell
& .venv\Scripts\dhruva-research.exe evaluate `
  --account owner-family --universe public-liquid-nse-v0 `
  --from 2016-01-01 --to 2026-01-01 --horizons 20 60 `
  --export "$Output\model-evidence.json"
```

Do not use `--strict-pit`: later retrieval cannot satisfy prospective known-at.
The report must say `PUBLIC RECONSTRUCTED HISTORICAL UNIVERSE`, evidence class
`PUBLIC_RECONSTRUCTED`, known-at `RETRIEVED_LATER`, raw-price basis and
readiness-v3 limitations. Current-owner-watchlist evaluation remains a separate
explicit mode.
