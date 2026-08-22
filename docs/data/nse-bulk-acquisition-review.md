# NSE bulk/monthly acquisition review

Status: implemented offline mapper and conservative acquisition planner.
Review date: 2026-08-22. No authenticated access, private endpoint discovery,
header spoofing, CAPTCHA automation, or browser-download automation was used.

## Conclusion

The official **Capital Equity Market - Exchange Monthly Report** can replace
daily bhavcopy downloads for DHRUVA's raw daily traded-equity bars. The reviewed
official data-definition workbook and one normal public sample establish a
`Transaction Data` sheet with a daily security row and these exact columns:

`Year`, `Month`, `Day`, `Date`, `Product`, `ISIN`, `Symbol`, `Issuer Name`,
`CIN of Issuer`, `Exchange`, `Platform`, `Instrument Type (Series)`,
`Listing Status`, `Available for Trading`, `Trading Status`, `Trade Term/Type`,
`Previous Close Price`, `Open Price`, `High Price`, `Low Price`,
`Last Traded Price`, `Close Price`, `VWAP (Turnover/ Total Traded Quantity)`,
`Trade Count`, `Traded Quantity`, and `Turnover (in Rs)`.

The sample contained daily rows for traded equities. It did **not** contain the
complete non-traded listed-security population. The monthly report therefore
replaces bhavcopy bars, but only partially replaces MII security files and
lifecycle evidence. DHRUVA maps `Product` values `Equity` and `Equity SME`,
requires `Trading Status=Traded`, `Available for Trading=Y`, `Exchange=NSE` and
a valid ISIN, and rejects an unknown workbook/header revision. The April 2026
sample had 67,421 data rows, of which 61,026 mapped to those two equity product
classes across 21 trading dates. Six mapped rows had a blank issuer name, so
company name remains optional and identity falls back conservatively to symbol.

Official sources:

- [Segment-wise Historical Reports](https://www.nseindia.com/static/regulations/segment-wise-historical-reports)
  publishes Exchange and Clearing Corporation reports, their data definitions,
  monthly Exchange reports from April 2016, and some annual ZIP links.
- [Exchange Data Definition](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Data_Definition_Equity.xlsx)
  defines the Exchange workbook.
- [Clearing Corporation Data Definition](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NCL_Equity_Data_Definition.xlsx)
  defines the clearing workbook.
- [Historical Reports](https://www.nseindia.com/static/resources/historical-reports-capital-market-daily-monthly-archives)
  links the EQ, security-wise, index, and TRI archive surfaces.
- [All Reports](https://www.nseindia.com/all-reports) exposes current and
  historical report selection plus `Multiple file Download`.

## Artifact field audit

| Artifact | Exact useful fields | DHRUVA conclusion |
|---|---|---|
| Exchange Monthly Report / Transaction Data | Date; Product; ISIN; Symbol; Issuer Name; Instrument Type (Series); Listing/Available/Trading status; previous close; OHLC; last; VWAP; trade count; traded quantity; turnover | Daily traded-equity bars fully replace daily bhavcopy; listed population/lifecycle only partial |
| Exchange Monthly Report / Mode of Trading | Year; Month; Day; Date; Exchange; Segment; number of trades and gross turnover for Algo, Non-Algo, Direct Market Access, Co-location, Internet Based Trading, Mobile, BOW/NOW and Smart Order Routing; total trades/turnover; percentage turnover for each mode | Daily market aggregates only; not mapped and not security OHLCV |
| Exchange Monthly Report / Category Data | Year; Month; Day; Date; Exchange; Segment; Buy, Sell and Net rupee turnover for proprietary, FII, mutual fund, bank, insurance, other DFI, NPS, retail, partnership, trust, HUF, NRI, QFI, other and total categories | Daily participant aggregates only; not mapped and not security OHLCV |
| Exchange Monthly Report / Top N Members | Year; Month; Day; Date; Exchange; Segment; turnover shares for top 5, 10, 25, 50 and 100 members; gross turnover | Daily concentration aggregates only; not mapped and not security OHLCV |
| Clearing Corporation Monthly / Settlement Data | Year; Month; trade Date; CCP; Symbol; Series; Settlement Type/Date/Number; Security ISIN Code; deliverable/delivered/short-delivery quantities and values | Settlement evidence only; no OHLCV or trade count |
| Clearing Corporation Monthly / Payin Data | Year; Month; Trade Date; CCP; Settlement Type/Date; pay-in funds and securities | Aggregate clearing evidence only |
| Clearing Corporation Monthly / Margin Data | Year; Month; CCP; Trade Date; Symbol; Series; Extreme Loss Margin; VAR Margin | Security/date margin evidence only |
| All Reports / Monthly Reports tab | Current UI exposes report families such as Security Category Impact Cost and CM Mode of Trading | Aggregates/reports, not a bhavcopy replacement |
| Security-wise Archives (Equities) | Symbol; Series; Date; previous close; OHLC; last; VWAP; total traded quantity; turnover; number of trades; deliverable quantity and percentage | Price history is useful per selected symbol, but ISIN/security ID and an all-security historical universe are absent |
| MII Security File (NSE Listed securities) | `FinInstrmId`, `TckrSymb`, `SctySrs`, `ISIN`, security name and status fields; optional listing date depending on revision | Complete snapshot point where retained; official public dissemination begins 2024-02-05 |
| Corporate Actions CSV | Symbol; company; series; purpose; face value; ex-date; record date | Separate, partial historical action evidence; completeness/revisions unproven |
| Historical NIFTY price index | Date; open; high; low; close; shares traded | Separate full-range price benchmark file |
| Historical NIFTY TRI | Date; Total Returns Index | Separate full-range total-return benchmark file |
| Exchange annual ZIP links | Link and year are public for selected 2016-2020 years | Contents/schema not sufficiently verified; no mapper and no substitution claim |
| `security.txt.gz` master specification | token; symbol; series; instrument type; permission; name; listing/expulsion/readmission dates; delete flag; face value; ISIN; per-market eligibility/status | Public documentation is clear, but a stable public historical bulk archive was not established; not planned |

The official [NSE Master Data specification](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE-Masters%20Data-v1.6.pdf)
documents `security.txt.gz`; documentation alone does not prove free historical
public availability. DHRUVA does not infer such an archive.

## Requirement classification for the lowest-work strategy

| Requirement | Classification | Basis |
|---|---|---|
| daily date | `FULLY_REPLACE_DAILY` | Exchange monthly Transaction Data |
| symbol | `FULLY_REPLACE_DAILY` | Exchange monthly Transaction Data |
| ISIN/security ID | `FULLY_REPLACE_DAILY` | ISIN is present; exchange security ID is not needed for bar continuity |
| series | `FULLY_REPLACE_DAILY` | `Instrument Type (Series)` |
| open/high/low/close | `FULLY_REPLACE_DAILY` | security/date OHLC |
| volume | `FULLY_REPLACE_DAILY` | `Traded Quantity` |
| turnover | `FULLY_REPLACE_DAILY` | rupee turnover |
| trade count | `FULLY_REPLACE_DAILY` | `Trade Count` |
| listed-security identity | `PARTIALLY_REPLACE_DAILY` | ISIN/symbol/series/issuer/status exist only on traded rows in the sample |
| corporate actions | `PARTIALLY_REPLACE_DAILY` | separate official export, but historical completeness/revisions are unknown |
| NIFTY price index | `FULLY_REPLACE_DAILY` | separate full-range official history |
| NIFTY TRI | `FULLY_REPLACE_DAILY` | separate full-range official history |
| lifecycle evidence | `PARTIALLY_REPLACE_DAILY` | annual MII checkpoints after 2024-02-05; gaps remain unknown |

## Lowest-work valid strategy

`MONTHLY_EXCHANGE_PLUS_ANNUAL_SECURITY_SNAPSHOT`

For the last complete 12/60/120 months as of 2026-08-22, the estimates are:

| Horizon | Exchange monthly files | Annual MII snapshots available in the range | Other exports (actions + price + TRI) | Total files | Lower-bound download-trigger clicks |
|---:|---:|---:|---:|---:|---:|
| 1 year | 12 | 1 | 3 | 16 | 16 |
| 5 years | 60 | 3 | 3 | 66 | 66 |
| 10 years | 120 | 3 | 3 | 126 | 126 |

The click estimate counts the click that triggers each retained download. It
excludes navigation, scrolling, date-picker and filter interactions. A corporate
actions custom-range export is counted as one; if the live UI limits the chosen
range, retain one original CSV per accepted chunk and record the actual larger
count. For dates before April 2016 the planner selects a hybrid and retains daily
archive work for that gap.

By comparison, daily bhavcopy plus daily security files require two raw files per
weekday session plus three separate exports. `Multiple file Download` can put the
two selected daily reports behind one download trigger, but selecting two
checkboxes plus the bundle link is three clicks per date. It does not materially
solve a multi-year acquisition.

## Exact owner workflow

1. Open [Segment-wise Historical Reports](https://www.nseindia.com/static/regulations/segment-wise-historical-reports),
   stay under `Capital Market` / `Equity Market`, and download each required
   **Exchange Monthly Report**. Do not substitute the Clearing Corporation file.
2. For lifecycle checkpoints, open [All Reports](https://www.nseindia.com/all-reports),
   expand `Historical Reports`, expand `Equities`, select the `Archives` tab,
   choose a date near each planned annual checkpoint, and download
   **CM - MII - Security File (.gz) (NSE Listed securities)**. Do not choose the
   NSE-plus-BSE-exclusive variant. If no file exists for a chosen holiday, select
   the nearest prior trading date and retain that date unchanged.
3. Open [Corporate Actions](https://www.nseindia.com/companies-listing/corporate-filings-actions),
   select `Equity`, choose the required custom range, and use `Download (.csv)`.
4. Open [NIFTY Indices Historical Data](https://www.niftyindices.com/reports/historical-data),
   select NIFTY 50 and the full required range; retain separate price-index and
   Total Returns Index CSVs. Never relabel the price index as TRI.
5. Put original bytes into the ignored manual drop. Do not rename contents,
   edit corrections in place, or place raw reports in Git. Run `inspect-drop`,
   `preflight`, `coverage`, and `bias` before reconstruction.

On All Reports, `Multiple file Download` is safe normal UI use: choose a date,
tick the desired report checkboxes, then click `Multiple file Download`. DHRUVA
does not automate it. For only bhavcopy plus MII, direct individual download
icons use fewer selection clicks; the multi-file control only reduces download
triggers.

## Scientific limitations retained

- Every historical file retrieved now remains `RETRIEVED_LATER` and
  `PUBLIC_RECONSTRUCTED`; no monthly file upgrades point-in-time semantics.
- Non-traded listed names, suspensions, delistings, symbol changes and revision
  history remain incomplete, especially before public MII dissemination.
- Corporate-action history completeness and numeric transformation details are
  not assumed.
- Weekday gaps are not silently labelled exchange holidays.
- Raw equities remain unadjusted. Price index and TRI remain different benchmark
  identities.
- Annual Exchange ZIPs and documented `security.txt.gz` are `UNKNOWN` for this
  use until their public historical contents and exact schemas are verified.
