# Historical evaluation universe and integrity runbook

This workflow is network-free. It inspects locally persisted source revisions;
it does not download data, authenticate to a vendor, scrape an exchange, or
create missing constituents/actions.

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

After an approved licensed dataset has been validated and imported through the
application port, inspect its logical id. Absence fails explicitly; it never
selects the current watchlist instead.

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
membership and mapping revisions, market/benchmark revisions, readiness-v2
fields, blockers, and a fingerprint. No graph database is involved.

The network-free shape expected from a future approved delivery is documented
in [the licensed-dataset import format](../data/historical-universe-import-format.md).
It is a format and validation contract, not an implemented importer or source
approval.

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

## Readiness v2

`EVALUATION_READY` additionally requires at least 2,000 sessions, warm-up, 104
ranking periods, mature 20/60-session outcomes, historical membership,
PIT-known-at, removals, delistings, instrument lifecycle, verified return basis,
benchmark history/basis, corporate-action integrity, licensing, and no critical
provenance gaps. Today's twenty names remain `DIAGNOSTIC_ONLY` even after a
ten-year price backfill.
