# Historical data provider diligence

Status: public-documentation review completed 2026-08-16. No subscription was
purchased, no account was created, no terms were accepted, and no authenticated
source was called. Product pages establish only the claims they state. Contract
rights, omitted capabilities, PIT integrity, price, and sample quality remain
unknown until written evidence is retained.

The canonical machine-readable matrix and questionnaire are emitted by:

```powershell
& .venv\Scripts\dhruva-data.exe providers --json --report provider-diligence.json
```

Scores are 0–5 and remain separate: technical fit 25%, research-integrity fit
35%, licensing clarity 20%, implementation ease 10%, and cost transparency
10%. The resulting 0–100 number only prioritizes diligence. A score never
approves a provider or substitutes for a contract/sample review.

## Decision matrix

| Provider/product | India | Inactive | Historical membership | Actions | PIT/revisions | Score | Disposition |
|---|---:|---:|---:|---:|---:|---:|---|
| NSE Data + NSE Indices | supported | unclear | supported | supported | unclear | 70 | owner diligence required |
| FactSet Global Prices/Symbology/PIT | supported | supported | unclear | supported | partial | 71 | owner diligence required |
| LSEG catalogue/indices/actions | supported | partial | supported | supported | unclear/partial | 69 | owner diligence required |
| Global Datafeeds | supported | unknown | partial | supported | unknown | 67 | owner diligence required |
| CMIE Prowess | supported | partial | supported | supported | unknown | 57 | investigate |
| Capitaline | supported | partial | unclear | supported | unknown | 50 | investigate |
| Accord ACE | supported | unknown | unknown | partial | unknown | 52 | investigate |
| Bloomberg Data License | partial public proof | supported | unclear | supported | supported/partial | 65 | low priority: enterprise/price unknown |
| TrueData | supported | unknown | unknown | supported | unknown | 51 | low priority |
| Accelpix | supported | unknown | unknown | unknown | unknown | 44 | low priority |
| Twelve Data | supported | unknown | not supported publicly | partial | unknown | 52 | reject for PIT-universe milestone |
| EODHD | India docs unclear | partial | not supported publicly | partial | unknown | 49 | reject for PIT-universe milestone |
| BSE Information Products | BSE only | unclear | partial | supported | unknown | 48 | low priority; insufficient for NSE alone |

The four owner-diligence comparators are intentionally different: an exchange
source (NSE), two broad institutional datasets (FactSet and LSEG), and an Indian
API vendor (Global Datafeeds). CMIE is the strongest India-specialist follow-up
if machine delivery and retention can be established. No provider is approved.

## Blocking questions for every proposal/sample

1. List exact NSE and BSE coverage, including inactive, suspended, merged,
   delisted, and re-listed securities.
2. Deliver historical constituent/universe intervals with both effective and
   source-publication timestamps, including removals and re-entry.
3. State price/action depth and distinguish raw, price-adjusted, total-return,
   price-index, and total-return-index series.
4. List every action type and its announcement, ex, record, effective,
   publication, and correction timestamps.
5. Explain identifier continuity across ISIN, symbol, venue, merger, demerger,
   and re-listing changes.
6. Demonstrate immutable revision identifiers, superseded-value retrieval, and
   correction publication time.
7. Confirm in writing personal/household use, automated local analysis, local
   backups, and indefinite retention.
8. Confirm whether derived research artifacts may remain after termination.
9. Provide schemas, representative samples, manifests, hashes, row/rate limits,
   and delivery support commitments.
10. Quote recurring and one-time price, taxes, minimum term, cancellation, and
    any deletion obligation.
11. Separate display, redistribution, benchmarking, derived-data, and model-use
    restrictions.
12. State backfill, restatement, incident, and historical-data SLA commitments.

Any unanswered critical question remains `OWNER_CONFIRMATION_REQUIRED`. A
sample may be preflighted under `EVALUATION_ONLY`, but authoritative import
requires explicit `LOCAL_RETENTION_CONFIRMED` or
`AUTOMATED_ANALYSIS_CONFIRMED` in the owner-reviewed manifest.

## Public evidence reviewed

- NSE: [EOD/historical](https://www.nseindia.com/static/market-data/eod-historical-data-subscription),
  [corporate data](https://www.nseindia.com/static/market-data/corporate-data-subscription),
  [indices](https://www.nseindia.com/static/nse-indices/index-data-subscription),
  and [data policy](https://www.nseindia.com/static/market-data/nse-data-policy).
- FactSet: [Global Prices](https://developer.factset.com/api-catalog/factset-global-prices-api),
  [Symbology](https://developer.factset.com/api-catalog/symbology-api), and
  [Fundamentals PIT](https://www.factset.com/marketplace/catalog/product/factset-fundamentals-point-in-time).
- LSEG: [catalogue](https://www.lseg.com/en/data-catalogue),
  [indices](https://www.lseg.com/en/data-catalogue/indices-benchmarks),
  [historical constituents](https://developers.lseg.com/en/article-catalog/article/building-historical-index-constituents),
  and [corporate actions](https://developers.lseg.com/en/article-catalog/article/workspace-corporate-actions-content-set-guide).
- Global Datafeeds: [actions](https://globaldatafeeds.in/fundamental-data-apis/),
  [constituent CSV](https://docs.globaldatafeeds.in/index-constituents-933202m0),
  and [historical EOD](https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/introduction/type-of-data-available/).
- India specialists: [CMIE Prowess](https://prowess.cmie.com/),
  [Capitaline](https://capitaline.com/Index.aspx), and
  [Accord](https://www.accordfintech.com/corporate).
- Other comparators: [Bloomberg Data License](https://professional.bloomberg.com/products/data/data-management/data-license/),
  [TrueData](https://www.truedata.in/market-data-apis),
  [Accelpix](https://support.accelpix.com/portal/en/kb/articles/pix-apis-realtime-and-historical-data-in-python),
  [Twelve Data](https://twelvedata.com/docs),
  [EODHD](https://eodhd.com/list-of-stock-markets), and
  [BSE tariff](https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf).

## Current decision

No acquisition is authorized. The smallest safe next owner action is a
non-binding written proposal and representative sample from NSE Data/Indices,
FactSet, LSEG, and Global Datafeeds using the questionnaire above. DHRUVA must
not scrape missing data or infer retention rights from public access.
