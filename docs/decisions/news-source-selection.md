# News source selection — GDELT approved, NSE deferred

| Field | Value |
|---|---|
| Date | 2026-08-03 |
| Decided by | Product Owner (drew) |
| Scope | MVP 1 news ingestion, provider selection |
| Status | Active |

> **This is a product-risk decision, not a legal opinion.** It records what the
> owner chose to do given the published terms he read, and why. It is not a
> claim about how those terms would be interpreted by anyone else, and it does
> not constitute legal advice.

## GDELT — approved for automated metadata ingestion

The GDELT Project's Terms of Use (<https://www.gdeltproject.org/about.html>,
"Terms of Use") state that "all datasets released by the GDELT Project are
available for unlimited and unrestricted use for any academic, commercial, or
governmental use of any kind without fee", and that redistribution is permitted
provided "any use or redistribution of the data must include a citation to the
GDELT Project and a link to this website (https://www.gdeltproject.org/)".

Access is via the documented DOC 2.0 API
(<https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/>) at
`https://api.gdeltproject.org/api/v2/doc/doc`, anonymously, with no key and no
recurring cost. The `mode=artlist&format=json` response returns article
*metadata* — URL, title, seen date, domain, language, source country — and no
article body, so the storage model DHRUVA already has is the storage model the
API supports.

The five selection conditions are therefore met: the access method is the
provider's own documented API; metadata storage is within terms that permit
redistribution outright; no body is required or available; the cost is zero; and
the attribution requirement is satisfied on every stored item.

**Conditions carried into the code.** Every persisted item names GDELT as its
source, carries `https://gdeltproject.org` as its attribution link, and names the
originating publisher's domain alongside it. DHRUVA never fetches the article
URL itself — it is stored for a reader to follow, and the adapter contains no
code that could crawl it.

## NSE — deferred, and not to be implemented

Automated NSE ingestion is **deferred in full**. No NSE adapter, no NSE content
persisted, no link-and-timestamp-only variant, no transient fetch for dashboard
display, and no NSE smoke-test code.

The owner reviewed the current official NSE Terms of Use and Copyright Policy
(<https://www.nseindia.com/nse-copyright>, updated 21/10/2025, and the linked
Terms of Use) and identified four concerns, any one of which would be enough:

1. Website content or data may not be stored in an electronic retrieval system
   without prior written permission. DHRUVA's news archive is exactly that.
2. Systematic or automated collection — scraping, extraction, harvesting — is
   prohibited. DHRUVA polls on a schedule.
3. NSE information may not be used for gaming, virtual trading or simulation
   activities. DHRUVA includes paper trading.
4. Content generally may not be aggregated, copied or duplicated unless
   expressly made available for download.

The Copyright Policy's personal/non-commercial allowance permits users to
"view, print copies and download" content. It does not clearly override the
restrictions above, and it does not obviously extend to database storage and
re-serving. The Hyperlinking Policy separately requires prior written permission
before a third-party site links to NSE.

**That an endpoint is reachable is not evidence of permission.** The RSS feed at
`nsearchives.nseindia.com` responds to anonymous requests and NSE publishes an
RSS index page. Neither fact bears on the terms, and neither was treated as
though it did.

**Reconsider only on new grounds.** This decision changes if the owner obtains
prior written permission from NSE, or enters an applicable licensed NSE data
agreement — for example through NSE Data & Analytics. Re-reading the same public
terms more optimistically is not new grounds.

## Implementation status (2026-08-06)

The GDELT adapter is implemented and its behaviour is proven against synthetic
fixtures: mapping, attribution, deduplication and every source-health outcome.
The owner's live smoke test reached the real endpoint and received HTTP 429,
which the adapter classified correctly as `RATE_LIMITED` without retrying.

**Live article-payload schema compatibility is therefore still unverified.** No
live response body has been observed. Until one is, the mapper's field handling
rests on hand-constructed fixtures written against GDELT's published field
names, and any claim that DHRUVA has "validated GDELT ingestion end to end"
would be overstating what was tested.

`language` and `sourcecountry` are read by the mapper and deliberately not
persisted; no column exists for them and none was added.

## Operator surface (2026-08-06)

`dhruva-news poll` and `dhruva-news show` are the only ways news enters or
leaves DHRUVA today. Both print the GDELT citation and its homepage link, and
both state that NSE filings are not an input. The poll command issues only the
documented DOC 2.0 endpoint, never fetches an article body, and stops issuing
requests entirely when the provider rate-limits it.

Search phrases are derived from approved watchlist names only. A bare exchange
symbol is never queried, every refused phrase is reported with its reason, and
no instrument is dropped from the report for lacking one. See
[`docs/runbooks/news-workflow.md`](../runbooks/news-workflow.md).

## Consequence for the product

MVP 1's news coverage is broader-source only until an official-filings route
exists on acceptable terms. Corporate filings are therefore *not* a DHRUVA input
today, and any dashboard must not imply otherwise. The
`OFFICIAL_FILING` and `EXCHANGE_NOTICE` source tiers remain in the domain because
they describe a real category of source, not because any such source is
configured — no filing feed is wired, and none may be added without revisiting
this note.
