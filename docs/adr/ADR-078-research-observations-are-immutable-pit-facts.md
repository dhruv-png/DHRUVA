# ADR-078 - Research observations are immutable point-in-time facts

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** -
- **Superseded by:** -

> Origin: the Product Owner-approved DHRUVA Completion Audit and Master Product
> Roadmap. This record starts its evidence clock without claiming that the
> current attention heuristic is an investment model.

## Context

DHRUVA can already resolve a watchlist digest and deterministic attention
ranking at one point-in-time cutoff. It can export a reproducible packet, but an
export exists only when an operator chooses to make and retain one. That cannot
support prospective evaluation: dates could be selected after seeing the state,
files could be lost, and nothing durable says which refreshes were healthy or
degraded.

The product roadmap requires an evidence-generating loop in which a model state
is frozen before outcomes are known and evaluated only after its horizon
matures. Today there is no validated investment-candidate model, no defensible
outcome horizon, and only a short market history. The first durable fact must
therefore preserve what exists truthfully--research attention--without backdating
or renaming it as a recommendation.

The Platform context already has a processed-event ledger. That table proves
idempotent event consumption for one run; it does not preserve what the research
product believed and must not acquire a second meaning.

## Decision

The Intelligence context owns an append-only **research observation ledger**.
The initial and only observation type is `ATTENTION_OBSERVATION`: an immutable
record of unusual observable price, volume and archived-news context. It is not
an investment recommendation, candidate rank, expected return, probability,
risk score, position-size decision, order or execution instruction.

Each observation records its account, effective PIT cutoff, recorded timestamp,
complete ranked universe, attention score/band/reasons, market-context
availability and fingerprint, archived-news content revisions, source-health
status, ruleset/schema revisions, universe fingerprint, whole-observation
fingerprint, run status and the deterministic research-packet body fingerprint.
The recorded timestamp does not participate in logical identity; a later retry
of identical account, cutoff and research state remains the same observation.

The idempotency identity is:

`account + observation type + PIT cutoff + whole-observation fingerprint`.

The whole-observation fingerprint covers the universe, ordered members, source
health, model/ruleset provenance and packet-body link. Creation time is never an
identity input. A transaction-scoped database advisory lock serialises one
account/type/cutoff stream. Identical retries return the existing fact. A
different fingerprint at the same cutoff appends a later row that explicitly
supersedes the previous row; a composite foreign key restricts supersession to
the same account/type/cutoff stream and a unique target prevents a fork.

PostgreSQL refuses `UPDATE`, `DELETE` and `TRUNCATE` on observation headers and
members. Corrections are later superseding observations, never edits. Both
tables carry `account_id NOT NULL`, receive ADR-074's permissive-v1 RLS policy,
and are accessed through an account-scoped Unit of Work and repository filters.

Future `CANDIDATE_RANKING` observations may reuse the header and add their own
model-specific member facts only after such a model exists and is separately
decided. Future realized outcomes are separate append-only facts referencing an
original observation/member and a matured horizon. They never update an
observation and cannot be inserted into its fingerprint.

Retention is indefinite because prospective evidence cannot be recreated. A
migration rollback can structurally drop the ledger, but must identify that
history loss explicitly and requires an export first. No ledger API may place an
order or be used as an execution ledger.

## Rationale

**A normalized header and member table** were chosen over one large JSONB body.
The header answers history queries without loading every member, constraints can
check counts, ranks and tenant keys, and individual ranked facts remain
inspectable. Deterministic fingerprints retain a compact proof of the exact
composite state and link it to the existing packet contract.

**Intelligence ownership** was chosen because this context assembles market and
news evidence into what the human sees. Marketdata continues to own bars,
Reference owns the universe, and Platform's processed-event ledger retains only
transport-consumer idempotency. A new bounded context would add a dependency and
migration surface before there is a second research model to justify it.

**A complete ranking** was chosen over storing only the brief's top-N. Evaluation
needs to know both what ranked highly and what did not. The brief remains a
presentation selection; it is not the evidence boundary.

Rejected: mutable “current observation” rows, because corrections would leak
backward; packet files alone, because retention and cadence remain discretionary;
using `recorded_at` as idempotency, because retries would duplicate facts;
reusing the processed-event ledger, because delivery state is not research
belief; creating outcome rows now, because no approved horizon has matured; and
calling current attention a recommendation, because it has no predictive claim.

## Consequences

Every valid explicit refresh now performs one additional local database
transaction after resolving its final research state. Healthy and degraded
research states accumulate; a market-data refusal that halts before a valid state
does not fabricate an observation.

The ledger grows indefinitely and cannot be cleaned with ordinary SQL. Test
infrastructure that commits refresh observations must temporarily remove and
restore the mutation triggers around its isolated database cleanup; production
has no such bypass.

Same-cutoff corrections remain visible as a chain, which is more complex than a
single row but is the evidence required to explain exactly what changed. A
history reader must display the observation type prominently so attention can
never be mistaken for a future candidate model.

Adding candidate rankings or outcomes requires migrations and tests, but not a
rename or reinterpretation of today's facts. Those later records inherit the PIT,
account isolation, provenance, idempotency and no-execution constraints decided
here.
