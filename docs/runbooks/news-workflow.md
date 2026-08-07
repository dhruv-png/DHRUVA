# Runbook — News Workflow (`dhruva-news`, `dhruva-digest`)

Covers: polling GDELT for news about the approved watchlist, reading the
point-in-time archive back, the watchlist digest composed from it, the
configuration these commands read, and what each exit status means.

> **NSE filings and exchange announcements are not an input to DHRUVA.**
> Automated NSE ingestion is deferred in full pending prior written permission
> or an applicable licensed data agreement. See
> [`docs/decisions/news-source-selection.md`](../decisions/news-source-selection.md).
> A news list produced by these commands is broader-source news only, and both
> commands say so in their own output.

> **Attribution is a condition of use.** GDELT's terms permit unrestricted use
> *provided* any use or redistribution carries a citation to the GDELT Project
> and a link to its website. Every stored item carries both, every rendering
> prints both, and anything built on top of this — a dashboard, an export, a
> report — must keep doing so.

## What the two commands do

| Command | Network | Writes | Answers |
|---|---|---|---|
| `dhruva-news poll` | yes | yes | "fetch what GDELT has about the watchlist and store it" |
| `dhruva-news show` | **no** | **no** | "what did DHRUVA know about this instrument at this instant?" |

There is **no scheduler**. Something has to decide when a poll runs, and until
that decision is made and reviewed, the owner makes it by typing the command. A
background loop against a free shared service is the easiest possible way to
lose access to it.

DHRUVA **never fetches an article body**. GDELT returns the publisher's URL,
DHRUVA stores it so a reader can follow it, and the adapter contains no code
that could crawl it.

## Polling

```bash
# Rehearse: print the plan and the exact requests, contact nothing.
uv run --project backend dhruva-news poll --account acct_<uuid> --dry-run

# Run it.
uv run --project backend dhruva-news poll --account acct_<uuid>
```

`--dry-run` is the safe first move on any day the configuration changed. It
prints the plan and every query and then stops, so the requests can be read
before they are issued.

### What a pass prints

1. **The search plan** — how many phrases in how many batches, each batch's
   phrases, every candidate phrase that was *refused* and why, and every
   instrument that is **UNQUERYABLE**.
2. **The queries** — the literal DOC 2.0 expression per batch.
3. **The batches** — one line each: health, HTTP status, `Retry-After` when the
   server supplied one, the reason, and how many items it offered. A batch that
   was never issued prints `NOT ISSUED`.
4. **The counts** — `fetched`, `inserted`, `unchanged`, `duplicates`,
   `analyses`, `links`, `unresolved`.
5. **The GDELT citation and the NSE notice.**

There is no separate "revised" count, because there is no update path: a
correction is stored as a new revision and therefore appears in `inserted`.
"Rejected" is reported as `duplicates`, which is what the deduplication ledger
actually decided.

### How queries are built

Phrases come from the approved active watchlist, and the rules are deliberate.

- **The company name is the first phrase**, with a trailing corporate suffix
  dropped when at least two tokens survive. "Adani Power Limited" becomes
  "Adani Power"; "Eternal Limited" stays whole, because "Eternal" is an
  adjective.
- **A bare exchange symbol is never a phrase.** `HAL`, `PNB`, `M&M` and `SBI`
  are precise inside NSE and ambiguous everywhere else. An alias that is exactly
  the ticker, or a single all-capitals token, is refused as `SYMBOL_SHAPED`. A
  single token shorter than four characters is refused as `TOO_SHORT`.
- **Case matters against the symbol.** `INDIGO` is the ticker and is refused;
  `IndiGo` is the name on the aircraft and is queried. `HDFC AMC` is upper case
  but has two tokens, and is exactly how the press writes it, so it is kept.
- **At most two phrases per instrument** — the company name and the short form
  the press uses. A third spelling spends a scarce request budget finding
  articles the first two already found.
- **Former names are queried inside their grace period** (one year), so
  "Zomato" still finds articles about `ETERNAL`. An expired one is refused as
  `EXPIRED` and reported rather than dropped.
- **Ordering is by canonical symbol**, then by a fixed candidate order. The same
  watchlist always produces the same requests, so a diff between two plans means
  the watchlist changed.

**Nothing is omitted silently.** Every refused phrase appears with its reason,
and any instrument with no usable phrase appears as `UNQUERYABLE`. Coverage
somebody believes in but does not have is the failure this prevents.

### Explicit override

Setting `DHRUVA_NEWS__QUERY_OVERRIDE` runs that one expression as the pass's
only query. The watchlist plan is **not built**, so there is no possibility of
issuing both. Unset the variable to return to watchlist mode; there is
deliberately no second switch, because two switches encoding one decision is how
a configuration comes to contradict itself.

```bash
DHRUVA_NEWS__QUERY_OVERRIDE='"State Bank of India"' \
  uv run --project backend dhruva-news poll --account acct_<uuid>
```

### Being rate-limited

If a batch comes back `RATE_LIMITED`, the pass **stops issuing requests**.
Every later batch is reported `NOT ISSUED`, and everything the earlier batches
returned is still ingested — being asked to slow down must not become data loss.
The command exits non-zero.

The correct response is to **wait and run it again later**, ideally with a
smaller `DHRUVA_NEWS__BATCH_SIZE` or a shorter `DHRUVA_NEWS__TIMESPAN`. Do not
run it in a loop, and do not add a proxy: GDELT publishes no rate-limit policy,
so there is nothing to comply with beyond sending fewer requests.

`RATE_LIMITED` is never converted into an empty success. "Nothing new today" and
"we were refused" are different facts and stay different.

Other unhealthy outcomes — a timeout, a malformed payload, an unrecognised
schema — are reported and the pass **continues**. One bad response says nothing
about the next query, and abandoning the pass would turn it into a day with no
news.

## Reading the archive

```bash
# What do we know about SBIN right now?
uv run --project backend dhruva-news show --account acct_<uuid> --symbol SBIN

# What did we know at 14:20 UTC on 6 August, over the preceding three days?
uv run --project backend dhruva-news show \
  --account acct_<uuid> --symbol HAL \
  --as-of 2026-08-06T14:20:00+00:00 --days 3 --limit 20
```

`--as-of` is a **knowledge** cutoff, not a publication one. Nothing DHRUVA first
saw after that instant is returned, which is what makes this answer usable in a
backtest. A correction observed after the cutoff stays invisible and the earlier
wording is returned instead; both revisions are stored, and neither overwrites
the other.

`--as-of` accepts any ISO-8601 instant. A value with no zone is read as UTC; a
value in another zone is converted. Omit it and it means now.

Each item prints its title, the publisher's URL, the source and its tier, the
GDELT citation and homepage, both timestamps labelled with whose they are, the
content revision, the event category, the sentiment verdict, and every
instrument the deterministic linker matched. An ambiguous match is marked. A
revision stored without an analysis says so rather than looking neutral.

`--limit` bounds what is **printed**, not what the database scans. Pushing the
bound into SQL means deciding there which revision of an item wins, and that
decision belongs with the point-in-time rules. It becomes a repository concern
when a window is large enough for the difference to be measurable.

An unknown symbol is refused with the list of approved symbols, so "no results"
and "you typed it wrong" stay distinguishable.

## Configuration

All keys take the prefix `DHRUVA_NEWS__`. There is **no API key, no token and no
proxy setting**: GDELT is anonymous, and the answer to being throttled is fewer
requests rather than the same number from somewhere else.

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `GDELT_ENDPOINT` | `https://api.gdeltproject.org/api/v2/doc/doc` | https only; no credentials, query or fragment | The documented DOC 2.0 endpoint |
| `QUERY_OVERRIDE` | *(unset)* | 1–512 chars, balanced quotes and brackets | Run one explicit expression instead of the watchlist plan |
| `BATCH_SIZE` | `8` | 1–25 | Phrases per request |
| `TIMESPAN` | `1d` | `15min`, `12h`, `1d`, `3w`, `1m` … | How far back one request looks |
| `MAX_RECORDS` | `75` | 1–250 | Articles one request may return |
| `TIMEOUT_SECONDS` | `30.0` | 1–120 | Whole-request timeout |
| `RESULT_LIMIT` | `50` | 1–500 | Items `show` prints by default |
| `LOOKBACK_DAYS` | `7` | 1–365 | Publication window `show` reads by default |

Everything is validated at composition. A process with invalid news
configuration refuses to start and names the offending field, rather than
starting and failing at the first request.

**Why eight phrases per batch.** The only live evidence DHRUVA has of GDELT's
throttling is an HTTP 429 on a single anonymous request, so the number that
matters is the number of requests: eight phrases turns the twenty-instrument
watchlist into single figures of requests rather than dozens. It is not the
largest possible batch either — one expression containing the whole watchlist is
unreviewable, and a single rate-limited response would lose the entire pass.

## Exit status

| Status | Meaning |
|---|---|
| `0` | The pass completed, or the read answered. An empty answer is still `0`. |
| `1` | A batch did not reach a usable answer — rate-limited, timed out, malformed — or infrastructure failed. |
| `2` | The command refused the input or the configuration and did nothing. |

A runbook step can therefore treat non-zero as "look at the output", and `2` as
"the command was asked for something it will not do".

## Known limitation

**Live GDELT article-payload schema compatibility is unverified.** The one live
smoke test reached the real endpoint and received HTTP 429, which the adapter
classified correctly. No live response body has been observed, so the mapper's
field handling rests on hand-constructed fixtures written against GDELT's
published field names. `scripts/gdelt_smoke_test.py --save-fixture out.json`
captures a real response when one gets through; sanitising it and replacing the
synthetic fixtures is the outstanding work.

---

## The watchlist digest (`dhruva-digest`)

```powershell
# What changed for the whole watchlist, right now?
uv run --project backend dhruva-digest --account acct_<uuid>

# What did we know about two instruments at a specific instant?
uv run --project backend dhruva-digest --account acct_<uuid> `
  --symbol SBIN --symbol HAL --as-of 2026-08-06T14:20:00+00:00 --days 3
```

**It reaches no network at all.** The digest is composed entirely from what
earlier `dhruva-news poll` runs already stored, so it works with the provider
unreachable, rate-limited, or simply not run today. It is also read-only: it
opens one transaction, reads, prints and exits.

**It reports; it does not advise.** Nothing in the output is a signal, a score,
a target or a recommendation, and the disclaimer at the top says so. The
categories, sentiments and instrument links were all decided by deterministic
rulesets at ingestion time and are printed back unchanged — the digest never
re-reads a headline, so it cannot disagree with the archive it summarises.

### How it is ordered

Both orderings are borrowed from rules that already exist rather than invented
here.

- **Between instruments**: by the most significant event stored about each one,
  using the event classifier's own rule order — "what a reader must not miss
  comes before what merely describes". So a governance finding outranks an order
  win. Instruments with nothing archived sort last, then alphabetically.
- **Within an instrument**: the same precedence, then newest first.

A `*` marks an entry the classifier gave a *specific* category.
`GENERAL_COMMENTARY` and `UNKNOWN` are unmarked, because counting commentary as
a finding would make every instrument look eventful.

### What it will not hide

Every watchlist instrument gets a section, including the silent ones — a missing
section is indistinguishable from a lost one, and "nothing happened" is the
usual answer. A section that has more items than it shows says how many it
withheld. An ambiguous instrument link is shown and marked rather than dropped.
Sentiment is **tallied, never averaged**: the mean of POSITIVE and NEGATIVE is
NEUTRAL, which is the one thing a split verdict does not mean.

An unknown `--symbol` is refused with the list of approved symbols, because a
typo would otherwise produce a confident, empty and entirely truthful-looking
report about an instrument DHRUVA does not follow.

### Market context

Each section also shows what the stored daily bars did, when any are knowable at
the cutoff:

```
SBIN  --  State Bank of India
    market   : close 143.5 on 2026-07-30, 4d before cutoff
               1d +1.06%   5d +5.51%   [AVAILABLE, 30 bars]
               volume 3,000,000   3.00x the mean of the prior 20 sessions
    events   : FRAUD_GOVERNANCE
```

**It is arithmetic, not analysis.** A one-day close-to-close change, a bounded
multi-day return, a volume ratio against the mean of the preceding sessions.
Every figure can be checked by hand against the bars it came from. There are
deliberately **no indicators** — a moving average or an oscillator would look
more sophisticated and would be a judgement DHRUVA has not earned the right to
make inside something that claims only to report.

**Nothing is computed from a bar that is not there.** A one-day change needs two
closes; a five-session return needs six bars. Where the history is short the
figure reads `n/a` and the section carries a note saying why. A five-day return
computed over three days would be a number whose label lies, so it is refused
rather than shortened.

**Staleness and history are separate facts.** `[STALE]` means the newest stored
bar is more than four days before the cutoff — a holiday, a halt, or an
ingestion that stopped running. `INSUFFICIENT_HISTORY` means there are fewer
than two bars. A series can be both, and both are reported: a single month-old
bar is stale *and* uncomparable.

| State | Meaning |
|---|---|
| `AVAILABLE` | Two or more bars; comparisons are possible |
| `INSUFFICIENT_HISTORY` | Exactly one bar; a close and nothing to compare it to |
| `NO_DATA` | Nothing knowable at this cutoff — not zero, not flat, absent |
| `[STALE]` | Newest bar older than the staleness bound, independent of the above |

**The cutoff governs both archives.** The news read and the bar read take the
same `--as-of`, so a section cannot pair yesterday's headline with tomorrow's
price. A bar *retrieved* after the cutoff is invisible even when its trading
date is earlier, which is what makes a digest usable as backtest evidence.

`--no-market` omits the market read entirely and reports archived news only.
When it is off but an instrument is absent from the result, the line reads
`not requested` rather than blank — a gap in the data and a gap in the request
are different facts.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--account` | required | Account the read is attributed to |
| `--as-of` | now | ISO-8601 knowledge cutoff |
| `--days` | `DHRUVA_NEWS__LOOKBACK_DAYS` (7) | Publication window before the cutoff |
| `--symbol` | whole watchlist | Canonical symbol; repeatable |
| `--max-items` | 5 | Items shown per instrument, 1–50 |
| `--sessions` | 5 | Sessions in the multi-day return, 1–60 |
| `--no-market` | off | Report archived news only; skip the daily-bar read |

Exit `0` on success, including a completely quiet window — a runbook that
treated a quiet day as a failure would be red more often than not. Exit `2` for
input the command refuses.

---

## The research snapshot (`dhruva-export`)

```powershell
uv run --project backend dhruva-export `
  --account acct_<uuid> --output snapshot-2026-08-06.json `
  --as-of 2026-08-06T14:20:00+00:00 --days 7
```

A sibling of `dhruva-digest` reading exactly the same path — same watchlist,
same news archive, same daily bars, same cutoff. What differs is only where the
answer goes. Read-only against PostgreSQL and against the network.

It answers, in a file that outlives the database: **what did DHRUVA know about
my watchlist at this exact cutoff, and what evidence supported it?**

### It will not overwrite

An existing output file is **refused** unless `--force` is given. A snapshot is
the thing somebody keeps in order to be able to say what they knew; a command
that silently replaced yesterday's would destroy the only copy of a fact at the
moment it became inconvenient. A missing parent directory is refused too, before
the database is read.

### The determinism contract

The file has two parts, and the split is the contract:

| Part | Stable? | Contains |
|---|---|---|
| `body` | **yes** — byte-for-byte | everything that is a function of database state, account, cutoff and schema version |
| `envelope` | no | `generated_at`, plus `body_sha256`, the schema version, the notice and the disclaimer |

Two exports of the same cutoff over unchanged data have **identical bodies and
identical fingerprints**, and differ in exactly one field. That is what makes a
diff between two snapshots meaningful rather than noise.

`body_sha256` is computed over the same canonical JSON the file is written in —
sorted keys, compact separators, UTF-8 — so a reader can recompute it from the
file alone and detect an edit without the database or this code.

### What a snapshot contains

Per instrument, in the digest's own significance order: identity, whether it had
news, the event categories and sentiment tally, the bounded list of items, and
the market context. Per item: the title, the publisher's URL, the source and its
attribution link, both timestamps, the content revision, the event category, the
sentiment, the deduplication verdict and every linked instrument with its
matched text. Plus the revision string of every ruleset that reached a verdict,
so "which classifier said that?" is answerable six months later.

Exact values — closes, percentages, ratios — are exported as **strings**. JSON's
only number is binary floating point, and a close of `143.50` that round-tripped
as `143.49999999999997` would make a snapshot disagree with the database it came
from. Parse them with a decimal type.

### What a snapshot deliberately does not contain

No raw provider payload, no article body, no credential, no machine-local path,
no account secret. The account appears as its stable surrogate identifier, which
identifies without revealing. A snapshot is a file somebody may email to
themselves; it should contain nothing they would mind having sent.

### Gaps stay explicit

`requested: false` means market context was switched off with `--no-market`;
`availability: NO_DATA` means it was looked for and the archive was empty. The
two are never collapsed, because a news-only snapshot must not read as evidence
that no prices existed. `items_withheld` reports truncation, so a bounded
section cannot be mistaken for a complete one.

### Options

Same selection flags as `dhruva-digest` — `--as-of`, `--days`, `--symbol`,
`--sessions`, `--max-items`, `--no-market` — plus:

| Flag | Default | Meaning |
|---|---|---|
| `--output` | required | File to write |
| `--force` | off | Replace the output file if it already exists |
