# ADR-074 — Row-level security policies are authored *and exercised* from the start, permissive in v1

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S06 design document `docs/design/S06-identity-secrets-audit.md`, §9. Implements the RLS half of ADR-004; nothing here replaces it.

## Context

ADR-004 decided it in Phase 0:

> Every domain table carries `account_id NOT NULL`. No global mutable state, no
> module-level caches keyed without account. PostgreSQL RLS policies authored
> from the start, permissive in v1.

Plan §6 schedules enforcement for S44 (Multi-Tenancy Activation) and gives S42 an
"RLS activation test suite". S44 is roughly forty subsystems away.

The gap between authoring a policy and enforcing it is where this decision lives.
A permissive policy is, by construction, a policy that never changes the result of
any query. Nothing observable distinguishes a correct permissive policy from a
policy that is silently wrong — a mistyped column, a session variable nobody
sets, a table that was never enrolled. The first time the difference becomes
visible is the day enforcement is switched on, which is the worst possible day to
discover it, and it is a day forty subsystems of accumulated tables away.

## Decision

RLS policies are authored in migrations for every table carrying `account_id`,
**permissive in v1** exactly as ADR-004 says.

**The scaffolding is exercised, not merely authored.** An integration test
against real PostgreSQL (ADR-058) enables a **restrictive** policy within its own
transaction, sets the session account, and asserts that another tenant's rows are
unreachable. The test proves the policies would bite, then rolls back. It is a
test of the policy definitions, not a change to the deployed posture.

**A test asserts that every table with an `account_id` column has a policy.** A
table added later without one fails the build rather than being discovered at
activation.

**The Unit of Work sets the session account variable** that the policies read,
because the Unit of Work owns the transaction (ADR-053) and RLS is scoped to the
session. No repository and no caller sets it.

## Rationale

**"Authored *and exercised*" is the whole content of this ADR.** ADR-004 already
requires authoring. Adding the exercise requirement is what converts an
unfalsifiable claim into a tested one, and it costs one integration test rather
than a change in posture.

**Enabling restriction inside a transaction and rolling back.** This is what lets
the policies be proven without changing what production does. The alternative —
a separate database configured restrictively — would test a different set of
policies from the ones that ship, which is the failure being guarded against.

**The Unit of Work sets the session variable.** RLS reads a session-scoped
setting, and a caller setting it independently could set it in one transaction
and read in another, producing a policy that appears to work in testing and
silently permits everything in production. Placing it where the transaction is
owned makes the two impossible to separate.

**A completeness test over `account_id` columns.** The realistic failure is not a
wrong policy; it is a table added in S19 by someone who never read this ADR. A
test over the schema catches that without anyone remembering.

Rejected: **deferring RLS entirely to S44** (rejected by ADR-004 already, and the
retrofit would touch every table the platform has by then); **enforcing
restrictive policies now** (ADR-004 says permissive in v1, and a single-operator
deployment would gain nothing while every query gained a way to return nothing);
**application-level tenant filtering instead of RLS** (a `WHERE account_id = ?`
that someone forgets is exactly the failure RLS exists to make structural).

## Consequences

**Every new table with an `account_id` needs a policy in its migration**, or the
build fails. That is friction on purpose, and it is the mechanism by which S44
inherits a complete set rather than an archaeology project.

**The deployed posture is still permissive**, so this ADR provides no isolation
today. It provides the evidence that isolation will work when activated. Anyone
reading "RLS is in place" should read it as "RLS is authored, tested and
inactive".

**S42's RLS activation test suite has something real to activate.** Its job
becomes flipping policies and running the existing test, rather than writing
policies for forty subsystems' worth of tables.

**Policies must be maintained through migrations they were not written for.** A
migration that renames or adds a tenant-scoped column must update the policy, and
ADR-055's reversibility declaration should cover the policy as well as the table.

**The completeness test is only as good as its definition of a tenant-scoped
table.** A table that stores an account reference under a different column name
escapes it. The naming convention `account_id` is therefore load-bearing, and the
test's docstring should say so.
