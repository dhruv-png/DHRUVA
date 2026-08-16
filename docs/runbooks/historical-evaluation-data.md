# Historical evaluation universe and integrity runbook

This workflow is network-free. It researches only retained public evidence,
validates owner-supplied files, and applies only a manifest whose licence and
integrity gates are READY. It does not download data, authenticate to a vendor,
scrape an exchange, purchase anything, or create missing facts.

## Research and preflight

```powershell
& .venv\Scripts\dhruva-data.exe providers
& .venv\Scripts\dhruva-data.exe preflight `
  --manifest C:\retained-delivery\manifest.json `
  --report C:\retained-delivery\preflight.json
```

Exit 0 and `import_decision=READY` require exact hashes/schemas, every critical
PIT/lifecycle/action/revision capability, and owner-confirmed local retention.
`QUARANTINED` or `REJECTED` is terminal for those bytes; do not edit an accepted
delivery in place or infer missing facts.

After reviewing the report, apply is a separate explicit local write:

```powershell
& .venv\Scripts\dhruva-data.exe import --apply `
  --account owner-family `
  --manifest C:\retained-delivery\manifest.json `
  --report C:\retained-delivery\import-result.json

& .venv\Scripts\dhruva-data.exe dataset-status `
  --account owner-family --dataset licensed-india-v1

& .venv\Scripts\dhruva-data.exe provenance `
  --account owner-family --dataset licensed-india-v1 --limit 100
```

The import is one transaction. Any identity, membership, action, bar,
provenance, or ledger failure rolls the whole delivery back. Identical retry is
a no-op. A corrected delivery uses a new source revision.

## Inspect current truth

```powershell
& .venv\Scripts\dhruva-reference.exe universe-readiness `
  --account owner-family `
  --universe current-owner-watchlist `
  --as-of 2026-08-16T12:00:00Z
```

Expected classification is `CURRENT_SELECTION_ONLY`, not survivorship-safe,
with UNKNOWN return/adjustment basis, unavailable corporate actions, NIFTY 50
`PRICE_INDEX`, and explicit blockers.

After a licensed dataset has been validated and imported, inspect its logical
universe id. Absence fails explicitly; it never selects the current watchlist
instead.

```powershell
& .venv\Scripts\dhruva-reference.exe universe-readiness `
  --account owner-family --universe licensed-india-v1 `
  --as-of 2020-01-03T10:00:00Z
```

## Evaluate an explicit universe

```powershell
& .venv\Scripts\dhruva-research.exe evaluate `
  --account owner-family --universe current-owner-watchlist `
  --from 2024-01-01 --to 2026-01-01 --horizons 20 60
```

For a licensed historical id, add `--strict-pit`. Every historical cutoff then
requires membership, identity mapping, stock bars, and benchmark revisions that
were known at that cutoff. Later-retrieved bars or later-published membership do
not masquerade as contemporaneous evidence. An insufficient-data refusal is the
correct result until the source provides genuine PIT observations.

The deterministic model-evidence export carries universe identity/source,
membership and mapping revisions, market/benchmark revisions, readiness-v3
fields, blockers, and a fingerprint. No graph database is involved.

The executable delivery contract is documented in
[the licensed-dataset import format](../data/historical-universe-import-format.md).
Its existence is not source approval.

## Stored semantics

- historical definitions and memberships are account-scoped, RLS-enrolled,
  append-only, effective-dated, known-at dated, and source revision keyed;
- identities are immutable internal instruments with effective symbol/provider
  mapping revisions; a rename does not create a new economic history;
- an ended membership remains queryable in its past interval; `is_delisted`
  preserves delisting evidence rather than deleting the security;
- corporate actions are global instrument facts with effective/ex/record dates,
  known-at, source revision, verification, ratios/value, and related identity;
- UNKNOWN Zerodha adjustment semantics remain UNKNOWN;
- a split-like discontinuity under UNKNOWN semantics blocks the affected return;
- `TOTAL_RETURN` requires verified adjustment and complete dividends;
- NIFTY 50 remains `PRICE_INDEX`; current output excludes dividends.

## Readiness v3

`EVALUATION_READY` additionally requires at least 2,000 sessions, warm-up, 104
ranking periods, mature 20/60-session outcomes, historical membership,
PIT-known-at, removals, delistings, instrument lifecycle, verified return basis,
benchmark history/basis, corporate-action integrity, licensing, and no critical
provenance gaps. Today's twenty names remain `DIAGNOSTIC_ONLY` even after a
ten-year price backfill.

`PUBLIC_RECONSTRUCTED` and `PARTIAL_PIT` state a useful but lower evidence
class explicitly. Readiness also carries known-at semantics, inactive-security
retention, archive gaps, reconstruction revision, and terms status. A later
manual download cannot become institutional `EVALUATION_READY`; use the
[public-data runbook](public-data-reconstruction.md) and select
`--universe public-liquid-nse-v0` explicitly.
