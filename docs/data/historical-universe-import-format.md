# Historical dataset manifest and canonical import format

Status: executable offline contract. Manifest schema is
`dhruva.historical-dataset-manifest.v1`; all canonical payloads are UTF-8 CSV
v1. The importer performs no network I/O and does not grant acquisition,
retention, redistribution, or provider approval.

## Immutable delivery

One directory contains `manifest.json` plus exactly these six roles:

| Role | Schema |
|---|---|
| `instrument_identities` | `dhruva.instrument-lifecycle.csv.v1` |
| `universe_definitions` | `dhruva.historical-universe-definition.csv.v1` |
| `universe_memberships` | `dhruva.historical-universe-membership.csv.v1` |
| `corporate_actions` | `dhruva.corporate-action.csv.v1` |
| `daily_bars` | `dhruva.daily-market-bar.csv.v1` |
| `benchmark_bars` | `dhruva.benchmark-history.csv.v1` |

Exact ordered columns are exported in
`dhruva.ingest.historical_dataset.CANONICAL_SCHEMAS`. Extra, missing, reordered,
or duplicate roles/columns fail. Every file carries its relative POSIX path,
lowercase SHA-256, byte size, row count, schema, and `text/csv` media type.
Paths may not be absolute, contain `..`/backslashes, leave the delivery root, or
resolve through a symlink.

`DatasetMapper` is the offline extension seam from provider-native files to
this contract. This milestone supplies `CanonicalCsvMapper` (identity mapping
for already-canonical deliveries) and `SyntheticFixtureMapper`. Both carry a
stable mapping revision in provenance. No live-provider mapper or transport is
implemented; Parquet requires a future versioned mapper/schema.

The manifest identifies provider/product, owner-reviewed licence reference and
status, acquisition instant, coverage, source revision, INR/NSE-or-BSE market,
Asia/Kolkata timezone, price/adjustment/return/benchmark bases, exact
`SOURCE_OBSERVED_AT` semantics, `APPEND_ONLY` policy, capability claims, files,
and notes. BSE bytes can be preflighted, but authoritative apply currently
refuses BSE because the personal-MVP identity domain remains NSE-only.

The optional `evidence_class` distinguishes `LICENSED_VENDOR`,
`PUBLIC_EXCHANGE_ARCHIVE`, `PUBLIC_RECONSTRUCTED`, `OWNER_PROVIDED`, and
`SYNTHETIC_TEST`. Legacy manifests infer only the prior licensed/synthetic
class; new public packs state it explicitly. Public reconstruction uses
`RETRIEVED_LATER`, never `SOURCE_OBSERVED_AT`, and persists evidence class in
the dataset ledger (Alembic `0023_public_evidence_class`).

Only `LOCAL_RETENTION_CONFIRMED` and `AUTOMATED_ANALYSIS_CONFIRMED` permit an
authoritative import. Provider identity and marketing claims cannot elevate the
licence state. `EVALUATION_ONLY`, unverified, restricted, and rejected content
stays quarantined.

`PUBLIC_RESEARCH_LOCAL_USE` additionally permits an explicit local import only
for a `PUBLIC_RECONSTRUCTED` manifest that declares `RETRIEVED_LATER` and makes
no PIT-known-at claim. Scientific incompleteness remains in `blockers`, while
hash/schema/semantic/source-selection/identity/licensing defects still refuse
the import. See the
[public reconstruction contract](public-exchange-reconstruction.md).

## Validation and staging

`dhruva-data preflight` opens no database. It streams bounded fields, verifies
bytes/hashes/counts/headers, and validates:

- UTC known-at and inclusive effective intervals;
- identity and membership non-overlap, re-entry, inactive/delisted references,
  and definition/revision alignment;
- stable security/action/related-security references;
- action ratios, cash/currency pairs, verification, and dates;
- positive Decimal OHLC, valid relationships/volume, coverage, market, return,
  benchmark, and adjustment claims;
- ordered bar revisions; a same-day correction needs a later known-at and a new
  source revision;
- suspicious 40% discontinuities; verified adjusted series require matching
  action evidence, while unknown/raw series receive a warning and no repair;
- all readiness claims: membership, removals, delistings, inactive securities,
  lifecycle, actions/dividends, publication timestamps, corrections, PIT,
  benchmark history, and owner licensing.

The deterministic report is `dhruva.dataset-preflight.v1` and ends in `READY`,
`QUARANTINED`, or `REJECTED`. Only `READY` can enter the apply transaction.

## Atomic import and provenance

`dhruva-data import --apply` uses one PostgreSQL transaction spanning the
Reference and Market Data repositories from the `dhruva.ingest` composition
root. It registers manifest/file revisions, stable internal instruments,
effective identity and membership revisions, action evidence, stock and
benchmark bar revisions, and one exact provenance link per source row. A
failure rolls back all of them.

Dataset, file, fact-provenance, and import-run ledgers are account-scoped,
RLS-enrolled, append-only, content-conflict detecting, and deterministically
identified. Identical retry adds nothing. A correction must use a new source
revision and appends a new fact; no old value is updated. Reports contain no
wall-clock-dependent values.

## Synthetic pack

```powershell
& .venv\Scripts\dhruva-data.exe sample --output .local\historical-test
& .venv\Scripts\dhruva-data.exe preflight `
  --manifest .local\historical-test\manifest.json `
  --report .local\historical-test\preflight.json
```

The generated three-year, six-security pack is conspicuously TEST DATA and
includes an IPO, delisting, symbol change, removal/re-entry, split, bonus,
dividend, merger, price-index benchmark, and corrected bar revision. Add
`--corrupt` to generate the negative hash fixture. It is not market data and
must never support investment conclusions.
