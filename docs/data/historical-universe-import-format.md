# Historical universe licensed-dataset import format

Status: format contract only. DHRUVA has no approved historical dataset and no
production importer. This specification is for a future owner-approved,
network-free CSV or Parquet delivery; it does not authorize acquisition, use,
storage, or redistribution.

## Dataset envelope

One delivery is an immutable directory containing:

```text
manifest.json
universe_definitions.csv | universe_definitions.parquet
universe_memberships.csv | universe_memberships.parquet
instrument_identities.csv | instrument_identities.parquet
corporate_actions.csv | corporate_actions.parquet   # optional only when absent is declared
```

`manifest.json` uses UTF-8 canonical JSON and contains:

- `schema`: `dhruva.historical-evaluation-dataset.v1`;
- `dataset_id` and provider `source_revision`;
- provider/legal source name and contract reference;
- generated-at timestamp in UTC;
- effective coverage dates and knowledge-time coverage dates;
- declared capabilities for removals, delistings, suspended securities,
  identity lifecycle, publication timestamps, corporate actions, dividends,
  adjusted prices, TRI, and correction history;
- licence/retention review reference and owner approval reference;
- every payload filename, media type, row count, byte count, and lowercase
  SHA-256 digest;
- a SHA-256 digest of the canonical manifest with its own digest field omitted.

All identifiers and source revisions are strings supplied by the source. DHRUVA
derives internal UUIDs deterministically only after validation. A corrected
delivery has a new dataset/source revision; it never overwrites an accepted
delivery.

## Universe definitions

Required columns:

```text
universe_id,label,kind,known_at,source,source_revision,
historical_membership_available,removals_included,delistings_included,
pit_known_at_available,instrument_lifecycle_available,licensing_confirmed
```

Every definition revision is a complete snapshot contract for its referenced
membership rows. Boolean coverage claims must agree with the manifest and the
retained diligence/contract evidence. An importer must never infer `true` from
the mere presence of rows.

## Universe memberships

Required columns:

```text
universe_id,source_security_id,effective_from,effective_to,known_at,
source,source_revision,membership_reason,is_delisted
```

`effective_to` is inclusive and may be empty for an open interval. `known_at`
is the earliest source-observable UTC instant, not download time. Re-entry uses
a new effective interval. Removed and delisted rows remain in the delivery.

## Instrument identities

Required columns:

```text
source_security_id,canonical_symbol,company_name,isin,exchange,
provider_instrument_token,valid_from,valid_to,known_at,source,source_revision
```

`source_security_id` represents stable economic/security identity. Symbol,
ISIN, exchange mapping, and provider token are effective revisions and must not
be used as eternal identity. Mergers/demergers may reference related stable
identities through corporate-action rows; they must not splice unrelated price
histories.

## Corporate actions

Required columns when the file is present:

```text
source_security_id,event_type,effective_date,ex_date,record_date,known_at,
ratio_numerator,ratio_denominator,cash_value,currency,
related_source_security_id,verification,source,source_revision
```

Supported event types are `SPLIT`, `BONUS`, `DIVIDEND`, `RIGHTS_ISSUE`,
`MERGER`, `DEMERGER`, `SYMBOL_CHANGE`, `DELISTING`, and `OTHER`. Empty optional
values remain unknown; they are never replaced with invented ratios or dates.
The presence of an action file does not by itself prove that adjusted prices or
total returns are correct.

## Fail-closed validation sequence

Before a future importer opens a database transaction it must:

1. validate the manifest schema and owner approval reference;
2. verify every byte count and SHA-256 digest;
3. reject duplicate logical source revisions with conflicting content;
4. validate UTC knowledge timestamps and effective intervals;
5. resolve every membership/action security id to exactly one immutable
   instrument identity at the relevant effective and knowledge time;
6. reject overlapping membership or identity intervals unless the source
   contract explicitly models a revision supersession;
7. reconcile manifest counts, coverage flags, and declared return semantics;
8. produce a deterministic dry-run report and dataset fingerprint;
9. append all accepted source facts atomically and idempotently;
10. emit readiness blockers for every absent or unproven capability.

CSV must be UTF-8 with a header and RFC 4180 quoting. Parquet columns use the
same names and logical meanings. Dates use ISO `YYYY-MM-DD`; instants use ISO
8601 UTC with `Z`. Decimal ratios and cash values must not pass through binary
floating point.

## Explicit non-capabilities

This contract does not define market-bar delivery, adjustment-factor
calculation, total-return construction, authenticated vendor transport, or
automatic acceptance. Those require separate source evidence and owner-approved
licensing. Until then, current evaluation remains `DIAGNOSTIC_ONLY`.
