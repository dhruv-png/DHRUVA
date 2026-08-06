# Runbook — News Workflow (`dhruva-news`)

Covers: polling GDELT for news about the approved watchlist, reading the
point-in-time archive back, the configuration both commands read, and what each
exit status means.

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
