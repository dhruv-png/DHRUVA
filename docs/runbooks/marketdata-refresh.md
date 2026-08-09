# Runbook — instrument resolution and bounded daily-history refresh

**What this covers.** `dhruva-marketdata coverage`, routine `refresh`, and the
separate `backfill-plan` / `backfill` historical workflow: resolving the
owner-family watchlist, inspecting full locally visible daily-bar coverage,
fetching the small range current market context needs, and explicitly acquiring
bounded deeper history.

**What it does not do.** No order placement, no positions or holdings, no
options, futures or intraday data. `coverage` makes no network call of any
kind. `refresh` calls Zerodha only when local coverage says it must, and it
reuses the session `dhruva-broker` already established — it never accepts a
credential of its own.

**The ₹500/month Kite Connect historical-data plan is not required to
validate this slice.** Every test in the repository runs against a
synthetic Kite instrument master and a synthetic daily-history provider, with
no network access at all. The plan is only needed the day you point `refresh`
at a real, live-logged-in Zerodha session — a decision this document does not
make for you.

---

## The intended flow, end to end

1. **Seed the watchlist** — `dhruva-reference seed --account owner-family`.
   No network, no credential. See `docs/runbooks/local-development.md`.
2. **Enrol and log in to Zerodha** — `dhruva-broker zerodha enrol` then
   `dhruva-broker zerodha login`. See
   `docs/runbooks/zerodha-authentication.md`. Only needed before your first
   `refresh` with a real session; `coverage` never needs it.
3. **Resolve instruments** — happens automatically inside `refresh`, the
   first time local coverage shows an unresolved mapping. There is no
   separate "resolve" command: instrument resolution and daily-bar refresh
   are one bounded, atomic step.
4. **Inspect coverage** — `dhruva-marketdata coverage --account owner-family`,
   any time, with or without a broker session. No network call.
5. **Bounded daily refresh** — `dhruva-marketdata refresh --account
   owner-family`, only when coverage shows something is missing.
6. **Inspect coverage again** — same command as step 4. `refresh` already
   prints the post-refresh coverage itself; this step is for later sessions.
7. **Digest and market context** — `dhruva-digest` reads the stored bars
   `refresh` wrote, with its own `known_at` cutoff, and makes no network call
   either.

## 1. Coverage — read-only, always safe to run

```powershell
dhruva-marketdata coverage --account owner-family
```

Reads the shared watchlist, the most recently archived Zerodha instrument
mapping, and locally stored daily bars. Makes zero external network calls.
Reports one line per watchlist instrument:

| Status | Meaning |
|---|---|
| `MISSING_MAPPING` | No current Zerodha instrument-master mapping is on record |
| `NO_DATA` | Mapped, but nothing has ever been ingested |
| `INSUFFICIENT_HISTORY` | Fewer than six complete daily bars — a current five-session return cannot be computed |
| `STALE` | Enough bars, but the latest one is older than the staleness bound |
| `READY` | Enough bars and fresh as of the cutoff |

followed by one aggregate line: `watchlist`, `mapped`, `unmapped`,
`with_bars`, `no_data`, `enough_history`, `insufficient`, `stale`.

`--as-of` sets the knowledge cutoff (default: now). A bar recorded after the
cutoff — even one dated before it — stays invisible, exactly as `dhruva-digest`
already behaves.

## 2. Refresh — bounded, and only when coverage says it must

```powershell
dhruva-marketdata refresh --account owner-family
```

1. Reads coverage first, exactly as step 1 does. If every watchlist
   instrument is already `READY`, the command prints that and exits —
   **zero provider calls**, and the broker session is never even checked.
2. Otherwise, it opens the `ENROLMENT` and `SESSION` credentials
   `dhruva-broker` already sealed. If neither exists, or the session was
   never established, or it has expired, the command refuses and names
   exactly which of the three is true — `ENROLMENT_MISSING`,
   `SESSION_MISSING` or `SESSION_EXPIRED` — with the `dhruva-broker`
   subcommand that fixes it.
3. Fetches one Zerodha instrument master and resolves the whole owner
   universe against it (one network call, regardless of how many
   instruments needed it), then archives the result through the same
   point-in-time archive `dhruva-reference` and the instrument-discovery
   slice already built.
4. Requests **daily candles only**, for the Nifty 50 benchmark plus every
   watchlist instrument not already `READY`, over one shared date range
   bounded to the last **twenty calendar days** (`settings.marketdata.
   bootstrap_lookback_days`) ending the day before the refresh instant. This
   is bootstrap for the *current* market context, not historical backfill —
   the bound is a hard configuration ceiling, never silently extended
   further back.
5. Ingests through the existing synchronized daily-history path. **The whole
   attempted batch is refused, atomically, if any one instrument is
   incompatible** — a stale session, a provider timeout, a malformed
   response, a missing benchmark session. Zero rows are persisted on
   refusal; every previously committed bar is untouched. This is not a
   partial-success mode; there isn't one.
6. On success, re-reads and prints coverage.

If twenty calendar days still yields fewer than six complete sessions for an
instrument, `refresh` reports it `INSUFFICIENT_HISTORY` and does **not**
extend the window further back to manufacture a longer answer.

If an instrument's mapping is still missing after resolution genuinely could
not find it in the provider's master, `refresh` names it explicitly —
`MISSING_MAPPING: <SYMBOL> has no current Zerodha mapping` — and excludes it
from the fetch rather than silently dropping it from the report.

## 3. Historical backfill - explicit, bounded, and resumable

Historical acquisition is deliberately absent from `dhruva-refresh` and from
routine `dhruva-marketdata refresh`. First produce a network-free plan:

```powershell
dhruva-marketdata backfill-plan --account owner-family --years 2
```

Or use an explicit earliest market date:

```powershell
dhruva-marketdata backfill-plan --account owner-family --from 2024-08-01
```

Exactly one target is required. `--years` is limited to 1-10 and `--from` is
also capped at ten calendar years. Planning reads the archived mapping and PIT
daily-bar coverage only; it does not inspect a broker session or call any
provider. It prints every deterministic chunk, mapping blocker, and exact
provider-request count.

Execution repeats the same local plan and then uses the existing read-only Kite
history adapter sequentially:

```powershell
dhruva-marketdata backfill --account owner-family --years 2
```

Targets are ordered NIFTY 50 first, then owner symbols alphabetically. Each
target runs newest-to-oldest in 365-calendar-day core chunks with seven days of
forward overlap. One safe transaction contains the target and NIFTY benchmark;
the benchmark's own chunk contains only itself. A later refusal leaves earlier
chunks committed. Rerun `backfill-plan` to see the reduced plan and `backfill`
to resume. Overlap and retries create no duplicate bars because normalized bar
content owns the append-only source revision.

Every historical response retains its actual modern `retrieved_at`. A 2020 bar
retrieved today is reconstructed history, not evidence DHRUVA possessed in
2020. Backfill never recalculates or mutates an existing
`ATTENTION_OBSERVATION`.

Coverage reports readiness at 20, 60, 120, 200 and 252 stored sessions,
remaining sessions to operational 252-session warm-up, and remaining sessions
to a stronger 2,000-session evaluation-depth target. The latter is only a data
depth check: today's twenty surviving owner stocks are not an unbiased
historical evaluation universe. Historical constituents and delisted securities
require an approved future source.

The current Zerodha NIFTY 50 mapping is represented as `PRICE_INDEX`, never
NIFTY 50 TRI. Its dividends are not fabricated. Zerodha historical bars carry
`adjustment=UNKNOWN`; DHRUVA retains raw provider observations and refuses
unexplained large discontinuities rather than silently treating them as
investment returns. Corporate-action-safe adjusted and total-return views stay
blocked until an approved, auditable source exists. Do not scrape NSE.

## No secret on this command, ever

`dhruva-marketdata` accepts attribution/time options plus the bounded historical
targets `--years` or `--from`. There is no `--api-key`, `--access-token`,
`--api-secret` or `--request-token`;
authentication is entirely `dhruva-broker`'s job, established once and reused
here read-only. A test reads the argument parser and fails the build if that
ever stops being true.

## Configuration

| Setting | Default | Bound | Meaning |
|---|---|---|---|
| `DHRUVA_MARKETDATA__BOOTSTRAP_LOOKBACK_DAYS` | `20` | `1`–`20` | Calendar days a refresh may look back to bootstrap current coverage. Narrowing is a valid operational choice; widening past twenty is not — the field cannot be set higher than its own default. |

## Troubleshooting

**"SESSION_MISSING" / "SESSION_EXPIRED" / "ENROLMENT_MISSING"** — run the
named `dhruva-broker zerodha` subcommand
(`docs/runbooks/zerodha-authentication.md`), then retry `refresh`.

**"REFUSED during instrument resolution"** — the Zerodha instrument-master
fetch or resolution failed; the message and the fields beneath it (printed to
stderr) name the reason. Nothing was written. Retry once the underlying
condition (network, rate limit, stale session) has cleared.

**"REFUSED during daily history ingest"** — the same guarantee applied to the
daily-candle fetch: one incompatible instrument refuses the whole attempted
batch, and the diagnostic names it. Previously stored data is unchanged;
retry the whole refresh.

**"the Nifty 50 benchmark has no current Zerodha mapping"** — the provider's
instrument master could not resolve the benchmark index this run.
Instrument-level ingestion has no calendar to validate against without it, so
the whole refresh refuses rather than guessing one. This should not happen
against the real Zerodha instrument master; if it does, treat it as a
provider-side data-quality incident.

**"already up to date; no provider call was made"** — not an error. Coverage
was already sufficient for the whole watchlist; nothing needed fetching.
