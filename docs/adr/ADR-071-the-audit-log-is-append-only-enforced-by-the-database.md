# ADR-071 — The audit log is append-only, enforced by the database, and publishes AuditRecorded

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S06 design document `docs/design/S06-identity-secrets-audit.md`, §6. Applies ADR-014's ledger pattern to actions; nothing here replaces it.

## Context

Plan §15.1 requires an append-only audit log for **every authentication,
configuration change, risk override, and order action**. Plan §12 fixes its
retention at *indefinite and immutable*. Plan §5 gives the Platform context
exactly one published event, `AuditRecorded`. Plan §15.2 lists an immutable order
audit trail among the compliance artefacts required before G5, and §2.2's
regulatory position rests on it.

ADR-014 already decided that orders and fills are an append-only immutable
ledger. The audit log is the same requirement applied to actions rather than to
trades, and the decision worth recording is whether it reuses that enforcement or
invents a second kind of immutability.

An audit log's entire value is evidential. A log that could have been edited is
not weaker evidence than one that could not — for a regulator or a post-incident
review, it is no evidence at all.

## Decision

The audit table is **append-only, enforced by PostgreSQL**. `UPDATE` and `DELETE`
against it fail at the database, not at a repository method that declines to
offer them. Two integration tests assert this against real PostgreSQL (ADR-058):
one attempting an `UPDATE`, one a `DELETE`, each expecting failure.

An audit record carries: **actor, action, subject, outcome**, `occurred_at` and
`recorded_at` (ADR-007), `account_id NOT NULL` (ADR-004), and the **correlation
id** already bound by S02.

An audit record **never carries a credential, a token, or any value the redaction
processor would strip** (ADR-037).

The audit row is written **inside the transaction of the action it records**, and
`AuditRecorded` is staged into the **outbox** in that same transaction (ADR-053
as amended by AR-001b, and the S05 relay).

**Retention is indefinite.** No expiry job, no archival tier, no pruning path
exists or may be added without a plan change.

Audited actions are the four classes plan §15.1 names, plus **reads of the
credential store**, which is the one place where knowing that a secret was
accessed is worth its cost.

## Rationale

**Database enforcement rather than a repository that offers no update method.**
Application-level immutability holds until someone opens `psql`, writes a
migration, or adds a method for a good reason. The threat model for an audit log
includes the operator, which is exactly the actor an application-level control
cannot bind.

**One transaction with the audited action.** The alternative — writing the audit
row after the action commits — produces two failure modes, and both are worse
than the coupling it avoids: an action that happened with no record of it, and a
record of an action that rolled back. ADR-065 reasoned identically about the
idempotency ledger.

**Through the outbox rather than publishing directly.** Publishing after commit
reopens the window AR-001b closed. Staging into the outbox means the event and
the audited change share one fate, and S05 already built the relay that drains
it, so this costs nothing new.

**Auditing credential reads but not reads generally.** Recording every read of
every table would multiply write volume by read volume for a benefit nobody has
asked for. Recording reads of the credential store is different in kind: the
question "was this secret ever accessed, and by whom" is the one asked after a
suspected compromise, and it cannot be answered retrospectively.

Rejected: **a trigger that rewrites attempted updates into new rows** (silently
succeeding on an operation that should fail teaches callers the wrong thing);
**an append-only log outside PostgreSQL** (a second store to back up, secure and
reconcile, for a table that must join to `account_id`); **soft deletion** (a
deleted flag is an update, which is the thing being forbidden).

## Consequences

**A mistaken audit row cannot be corrected**, only followed by a compensating
one. That is the intended property and it will at some point be inconvenient.

**The audit table grows without bound**, by decision. Its index strategy and its
effect on backup duration become real questions well before S44, and neither is
addressed here.

**Writes get slower**, because an audited action now writes two extra rows — the
audit record and its outbox entry — inside its own transaction.

**Migrations touching the audit table are constrained.** ADR-055's expand/contract
discipline still applies, but a contract step that drops a column drops evidence;
such a migration should be treated as a plan-level question rather than a routine
reversibility declaration.

**Every subsystem that performs an audited action inherits an obligation.** S23's
order path, S06's own authentication path, and any configuration change must
write one. A test asserting that an audited action without a record fails belongs
with each of them, not here.
