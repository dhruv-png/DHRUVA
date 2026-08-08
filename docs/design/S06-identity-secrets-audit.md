# S06 — Identity, Secrets Vault & Audit

> **Status:** Approved and in progress. ADRs 070–074 are accepted; delivery
> steps 1–9 are complete. RLS remains permissive but exercised; S06 secret
> material is covered by both redaction strategies; authentication and
> authorisation metrics use closed, bounded-cardinality labels. Release
> validation and documentation reconciliation are next.
>
> Everything below is justified from the Master Project Plan (§5 C9, §6 the S06
> catalogue row, §8 G0, §12 retention, §15.1 security controls) and from
> accepted ADRs. Where the repository does not decide something, §14 asks rather
> than assumes.

---

## 0. The constraint that shapes everything else

The plan states it in one line, as ADR-020's consequence:

> **"A Secrets/Vault module (S06) precedes any broker integration."**

That is not a sequencing preference. Until S06 exists there is nowhere a Kite
API secret may live that ADR-020 permits, so **S23 (Broker Gateway) cannot
begin**, and **S07 (Instrument Master)** — which ingests the daily Kite dump
through an authenticated call — lists S06 as a dependency too. S06 is the
subsystem two others are waiting on, and the catalogue rates it HIGH risk at
size L over six weeks.

A second sentence, ADR-026, decides *when*: **no live capital before Gate G5.**
S06 is therefore built long before any real credential exists, which is the best
possible time to build it. Every control can be proven against synthetic
secrets, and the first real credential arrives into a system that already
refuses to log it.

---

## 1. What S06 is for

The catalogue row (plan §6, S06) names four deliverables:

> **JWT+refresh (OIDC-ready), envelope encryption for broker credentials,
> append-only audit log, RLS scaffolding.**

They are one subsystem because they answer one question in four parts: *who did
this, what were they allowed to do, what did they actually do, and what did the
system hold on their behalf?* Split across subsystems they would drift — an
audit log that does not know who the actor was is a list of events, and a
credential store with no audit trail is a box nobody can prove was not opened.

What S06 is **not**: it is not authorisation for trading *decisions* (ADR-012's
risk gate, unbypassable for different reasons), and it is not an identity
*provider*. "OIDC-ready" means token issuance is a port, so delegating to an
external provider later is an adapter swap — the same argument ADR-067 made for
transports.

---

## 2. Finding from the review: an open G0 checklist item

G0's checklist (plan §8) contains:

> - [ ] Secrets encrypted at rest; a deliberate log-leak test proves redaction
>   works

**Half of that item is met and half of it cannot have been.** The redaction half
exists and is tested — `SecretValue` in `dhruva.shared.config.secret`, the
redaction processor in `dhruva.shared.logging.processors`, and the deliberate
leak tests in `tests/unit/shared/test_logging_redaction.py` and
`test_secret_value.py`. The encryption half does not exist at all: a search of
`backend/src/dhruva` for `encrypt` returns nothing, because no subsystem before
S06 stores a secret.

This is recorded as a finding rather than a defect. Nothing was skipped — the
item is simply not satisfiable before the subsystem that owns encryption at
rest, and S06 is that subsystem. **S06's definition of done should close it
explicitly**, and §13 step 4 is where.

> **Closed at step 4.** Both halves now hold. The encryption half is the
> credential store: `credential` rows carry AES-256-GCM ciphertext and a wrapped
> data key and nothing else, the master key lives in process configuration and
> never in the table, and a read returns ciphertext — obtaining a plaintext is
> one named function that takes a `KeyProvider`. The redaction half was already
> met and is extended here: an opened credential comes back as a `SecretValue`,
> registered for value-based redaction, so a later accidental interpolation into
> a log line is masked rather than printed.

### A defect found while building step 4

The step-3 `credential` migration carried no `version` column. ADR-057 is
unambiguous — *every aggregate carries a `version` column*, and updates match on
it so that a lost update raises rather than being overwritten — and a credential
is an aggregate: it is loaded, re-sealed on rotation, and stored again.

Without the column the repository could not make the promise the `Repository`
protocol publishes, and two concurrent rotations would have left a row wrapped
under a data key whose plaintext nobody recorded. Corrected in the migration
itself rather than in a follow-up revision, because it had not been committed,
let alone released; ADR-051 fixes release tags, not unmerged migrations.

Recorded here rather than passed over because the omission was against an
accepted decision, and a decision that can be missed silently once can be missed
again.

---

## 3. Where S06 lives: the existing Platform context

**No new bounded context.** The plan already assigns all four deliverables to an
existing one — §5, C9:

> | C9 | **Platform** | Auth, accounts, secrets, **persisted account-scoped
> configuration** (feature flags, preferences, limits), audit, jobs |
> `AuditRecorded` | — |

`dhruva.contexts.platform` exists, S05 built its messaging and scheduling
packages inside it, and it is already listed in `.importlinter`'s `containers`
and in `ALLOWED_CONTEXT_DEPENDENCIES` as `"platform": frozenset()` — depending
on no other context.

Three consequences worth stating, because each removes work I might otherwise
have invented:

1. **The boundary map does not change.** No ADR-027 revision, no edit to
   `ALLOWED_CONTEXT_DEPENDENCIES`, no new `.importlinter` container.
2. **Platform is already a leaf**, so "identity must not know about trading" is
   enforced today rather than needing a new rule. A platform module cannot
   import a position size because it cannot import Trading at all.
3. **The `Consumes` column is empty and must stay empty.** An audit log that
   imported the contexts it records would invert the graph.

New packages, following the layout S05 established:

```
dhruva/contexts/platform/
├── domain/identity/            value objects, ports, policies -- no I/O
├── domain/audit/               the audit record and what makes one valid
├── application/                use cases
├── infrastructure/crypto/      KeyProvider adapters
├── infrastructure/persistence/ credential, audit, refresh-token repositories
└── interfaces/                 HTTP surface; may not import infrastructure
```

The four-layer ordering inside the context is already enforced by
`.importlinter`'s `layers` contract, and `interfaces-never-touch-adapters`
already names `dhruva.contexts.platform.interfaces`. Both apply to these
packages the day they are created, with no configuration change.

**Note on ADR-031.** C9 owns *persisted, account-scoped* configuration. The
master encryption key is **process** configuration and therefore belongs to
`dhruva.shared.config` (ADR-033, boundary rule R5) — not to a platform table.
S06 must not blur that line: the key that decrypts the vault cannot live in the
vault.

---

## 4. Envelope encryption (proposed **ADR-070**)

ADR-020 already fixed the shape, and plan §15.1 repeats it:

> Kite API secret and access tokens are envelope-encrypted (per-record data key,
> master key from environment/KMS). Tokens never appear in logs; a log redaction
> filter is applied globally and tested.

S06 implements that sentence and adds only what it leaves open.

**The hierarchy.** The master key arrives from the configuration provider
(ADR-033 — never the repository, never a database column). Each stored
credential gets its own **data key**; the data key encrypts the credential, and
the master key encrypts the data key. The row stores ciphertext and the wrapped
data key, never the master key and never a plaintext data key.

**Why per-record data keys rather than encrypting with the master directly.**
Three reasons, and the third is the one that matters operationally.

1. A single key encrypting every record is a single key whose compromise is
   total.
2. Rotating the master key means re-wrapping N small data keys, not
   re-encrypting N credentials — the difference between a maintenance window and
   a migration. Plan §6 puts secret rotation in S42 (Security Hardening &
   Threat Model), so rotation *will* be asked for, and the hierarchy is what
   makes it cheap when it is.
3. It bounds blast radius per record, which is what lets a single leaked
   ciphertext be reasoned about at all.

**A port, not a library call.** `KeyProvider` is a domain port that wraps and
unwraps a data key. The environment-backed adapter is what S06 ships; a KMS
adapter is the substitution ADR-020 anticipates by writing "environment/KMS".
Neither the domain nor the repository knows which one it has.

**The cipher is ADR-070's to fix**, and it introduces the first new runtime
dependency since celery — see §14.

---

## 5. The credential store

A repository over an encrypted table, following S04's four-layer mapping exactly
(ADR-052): domain ← factory ← record ← model ← row. The domain object holds a
`SecretValue`, which the logging stack already knows to keep out of records
(ADR-037); the persistence record holds ciphertext and never a plaintext.

**The store cannot return a plaintext by accident.** Decryption is an explicit
call taking the `KeyProvider`, so a caller that merely *reads* a credential row
gets ciphertext. Making the plaintext path require an argument is what stops it
from being the default.

`account_id NOT NULL` on every row per ADR-004 — including here, because a
credential belongs to a tenant and this would be the worst place for
multi-tenancy to leak. Plan §6 (S44 — Multi-Tenancy Activation) later wants
"per-tenant token vaults"; the column is what makes that a policy change rather
than a re-model.

Transactions are the Unit of Work's (ADR-053); the repository never commits.

### 5.1 One broker, two secret lifecycles (ADR-077)

The store originally held **one credential per broker per account**, which was
right until the first broker was wired. Kite Connect needs two secrets whose
lifecycles have nothing in common: long-lived application material the owner
enrols and rotates rarely, and a short-lived access token that a login mints and
that expires on the broker's schedule.

Credentials therefore carry a closed **purpose** — `ENROLMENT` or `SESSION` —
and it participates in identity, uniqueness *and* the cryptographic binding:

- uniqueness is `(account_id, broker, purpose)`;
- `credential_associated_data` binds `(credential_id, account_id, broker,
  purpose)`, so a session ciphertext moved into the enrolment row fails to open
  rather than being read as the owner's API secret;
- the scheme tag moved from `dhruva.credential.aad.v1` to `…v2`, so anything
  sealed before the distinction existed fails closed;
- `CredentialRepository.get` takes a purpose. There is no lookup that omits one.

The purpose is generic rather than Zerodha-specific, for the reason ADR-003 gives
about the rest of the platform: these are properties of secrets, and any broker
with an application credential and a login has both.

**Rotation and audit follow from the split.** A daily login re-seals only the
session row, so `rotated_at` on the enrolment credential keeps answering "when
did I last rotate my API secret" rather than "when did I last log in", and
ADR-057's conflict detection no longer fires on routine logins. Audit records
name a credential by `credential_id`, which remains unique; what changes is that
two credentials for one broker are now distinguishable in the trail.

**Session expiry is deliberately absent** from the credential aggregate. This
decision separates lifecycles; designing the broker-session model — expiry,
refresh, what counts as a live session — belongs with the login use case that
will produce one.

**What this does not do.** It stores no Zerodha material, opens no broker
session, makes no network call and fetches no market data. It is the schema and
domain foundation the enrolment and login slice needs, and nothing more.

---

## 6. Append-only audit log (proposed **ADR-071**)

ADR-014 already establishes the pattern for the order ledger — *orders and fills
are an append-only immutable ledger*. The audit log is that same decision
applied to actions, and it should reuse the same enforcement rather than invent
a second kind of immutability.

**What must be audited** is enumerated by plan §15.1, so it is not a judgement
call:

> Append-only audit log for **every authentication, configuration change, risk
> override, and order action**.

Four classes of action, all of them writes. Auditing reads is not asked for and
is not proposed — see §14.

**Append-only means the database refuses**, not that the code declines. A table
whose immutability is a convention is one `UPDATE` away from being worthless as
evidence, and its value is entirely evidential. The mechanism — a rule, a
trigger, or revoked grants — is ADR-071's to fix, and each needs the same two
tests: an `UPDATE` and a `DELETE` must both fail against real PostgreSQL
(ADR-058), never against a fake.

**Retention is already decided.** Plan §12: *"Orders/audit: indefinite,
immutable."* So there is no retention policy to design and no expiry job to
write, and any future proposal to prune the audit log is a plan change.

**What a record carries.** Actor, action, subject, outcome, `occurred_at` and
`recorded_at` (ADR-007 applies here as everywhere), and the correlation id S02
already binds — so an audit entry joins to the log lines and events of the same
request without anybody inventing a second identifier.

**What it must not carry**: the credential, the token, or anything the redaction
processor would strip. An audit log is written to be read widely, which makes it
the worst possible place for a secret.

**Written in the actor's transaction**, following ADR-065's reasoning about the
idempotency ledger: an audit row committing separately can be missing for work
that happened, or present for work that rolled back. Both are worse than an
audit log that is simply part of the unit of work.

**`AuditRecorded` is published through the outbox.** Plan §5 gives C9 exactly one
published event. S05 already built the mechanism, and it is the right one for
the same reason: staging the event in the caller's transaction means the event
and the audited change share one fate.

---

## 7. Tokens (proposed **ADR-072**)

Plan §15.1 decides more here than I expected, so most of this section is
implementation of an existing decision rather than a proposal:

> **AuthN** — JWT access (**15 min**) + **rotating** refresh token; TOTP 2FA
> mandatory for any account with order permissions.
> **Transport** — TLS everywhere; HSTS; secure + httpOnly + SameSite cookies.

**Issuance is a port.** `TokenIssuer` mints and verifies; the local
implementation signs JWTs and an OIDC provider is the adapter "OIDC-ready"
anticipates. Callers hold the port, never a library.

**Two lifetimes, one reason.** The access token is short-lived (15 minutes, per
the plan) because it travels on every request and its compromise is bounded by
its expiry. The refresh token is long-lived because forcing re-authentication
every fifteen minutes is how people end up storing passwords in scripts.

**Refresh tokens are revocable; access tokens are not.** A JWT verified by
signature alone cannot be withdrawn before it expires — that is the trade for
statelessness. So revocation lives on the refresh token, which is stored, and
the 15-minute access lifetime is what bounds the window. The alternative, a
revocation list checked per request, reintroduces on the hot path exactly the
database lookup the JWT existed to avoid.

**Rotation on use**, as the plan's word "rotating" requires. A refresh token
presented once is replaced; presenting the old one again is evidence of theft
rather than of a retry, and it invalidates the chain. That needs stored lineage,
which is the second reason refresh tokens are rows and not just signatures.

**Distinct from broker tokens.** ADR-021's daily session/token lifecycle governs
the *Kite* access token, which S23 owns and which lives in the credential store
of §5. A user's refresh token and a broker's daily access token are different
things with different lifetimes; conflating them in one table would be the
easiest mistake in this subsystem.

---

## 8. Authorisation and 2FA (proposed **ADR-073**)

Also decided by plan §15.1, and worth its own ADR because it constrains S23 and
every UI subsystem:

> **AuthZ** — Role-based, **defaulting to deny**; **order placement is a
> separately granted permission**.
> TOTP 2FA **mandatory** for any account with order permissions.

Three commitments fall out, and each is testable now:

1. **Deny by default.** An endpoint with no declared permission is refused, not
   allowed. The test is that adding a route without a permission fails — a
   default that only holds when someone remembers it is not a default.
2. **Order placement is its own permission**, never implied by an
   administrative or "full access" role. This is the authorisation-layer
   counterpart to ADR-012: the risk gate makes order placement unbypassable, and
   this makes it unimplied.
3. **2FA is a property of the permission, not of the user.** Granting order
   permission to an account without TOTP enrolled must fail. Expressed the other
   way round — a checkbox someone remembers to tick — it is a control that is
   correct until the first hurried afternoon.

S06.7 adds exactly one administrative capability,
`manage_authorisation`. Grant and revoke use cases enforce that capability,
same-tenant activity and enrolled TOTP inside the mutation transaction. A
tenant-scoped PostgreSQL advisory lock serialises these changes; revocation may
not remove the last active, TOTP-enrolled manager. Granting a TOTP-protected
permission to a shared role requires every current holder, including a disabled
holder, to have enrolled TOTP. The first manager is deliberately not bootstrapped
through this path; a later explicit one-time command owns that ceremony.

---

## 9. RLS scaffolding (proposed **ADR-074**)

ADR-004 already decided it:

> Every domain table carries `account_id NOT NULL`. No global mutable state, no
> module-level caches keyed without account. **PostgreSQL RLS policies authored
> from the start, permissive in v1.**

**Closed at step 8.** The S06 tables were enrolled as they were created, and
migration `0012` enrolls `daily_snapshot`, the one tenant-owned table that
predated ADR-074. The deployed predicates remain `true`: S06 does not activate
restrictive production RLS. `outbox.account_id` is nullable event provenance,
not tenant ownership, so unattributed system events remain valid.

"Permissive in v1" is the part needing care. A permissive policy that is never
exercised is a policy nobody knows works, and its first real test would be the
day it is switched on — which plan §6 schedules for S44, roughly forty
subsystems later. So the scaffolding must include a test that a **restrictive**
policy actually bites: set the session's account, read, and observe another
tenant's rows are unreachable. Plan §6 gives S42 an "RLS activation test
suite"; S06's job is to ensure that suite has something real to activate.

The session variable carrying the current account is
`dhruva.current_account_id`, the name established by migration `0007`. The Unit
of Work sets it with PostgreSQL's transaction-local `set_config` form before an
account-owned repository can be reached. Tests prove commit and rollback both
clear it before the pooled connection is reused. Restrictive-policy coverage
runs under a temporary non-owner, non-superuser role so PostgreSQL's owner,
superuser and `BYPASSRLS` exemptions cannot make the test pass vacuously.

---

## 10. Errors, observability and redaction

**Errors.** ADR-038 keeps the taxonomy closed with stable codes.
`PermissionDeniedError` (`DHR-PRM-001`) already exists in
`dhruva.shared.errors.taxonomy`; S06 needs authentication failure, token expiry
and token revocation. Whether these are new codes or subclasses is ADR-072's to
settle, and adding a code is a public-contract change the existing pinning test
already enforces.

**Authentication failures must not distinguish "no such user" from "wrong
password"** in anything a caller can observe. The distinction is useful to an
attacker and to nobody else. It may of course be recorded in the audit log,
which is the point of having one.

**Observability.** ADR-035 applies unchanged. Two S06-specific additions:
authentication outcomes are **metrics**, because a spike in failures is the
signal that matters and reconstructing it from logs afterwards is too late; and
the redaction processor gets tested against S06's own types under both of
ADR-037's independent strategies, beside the existing redaction suite.

**Delivered at step 9.** `dhruva_platform_authentication_attempts_total` counts
login and refresh outcomes, and
`dhruva_platform_authorisation_mutations_total` counts grant and revoke
outcomes. Both expose only a closed operation enum and the closed outcomes
`succeeded`, `refused` and `error`. Subjects, account IDs, roles, permissions,
free-form reasons and every secret-bearing value are structurally absent from
the metric port. The deliberate leak tests cover password hashes, access and
refresh tokens, broker secrets and TOTP material through sensitive field names
and through registered-value interpolation.

---

## 11. Deliberately deferred debt

| ID | Item | Why deferred |
|---|---|---|
| TD-S06-1 | No external identity provider | The port exists; the adapter arrives when there is an operator population to federate |
| TD-S06-2 | RLS policies remain permissive | ADR-004 says v1 is permissive; enforcement is S44 |
| TD-S06-3 | Master key from the environment, not a KMS | ADR-020 permits either; an adapter swap |
| TD-S06-4 | No key-rotation job | The hierarchy makes rotation cheap; the job belongs to S42, which owns secret rotation |
| TD-S06-5 | **No performance budget for token verification** | It sits on every authenticated request and needs one. ADR-060 A1's Linux figures were unavailable when this was written; set the budget from the first E4 run |
| ~~TD-S06-6~~ | ~~**Envelope ciphertexts carry no associated data**~~ | **Closed at step 4**, as this row said it would be. `encrypt_secret`/`decrypt_secret` take `associated_data` as a required keyword argument, and `credential_associated_data` derives it from the credential's identity, account, broker and -- since ADR-077 -- purpose. An integration test copies one row's ciphertext and wrapped key onto another with SQL and asserts the result no longer opens, while the victim row still does |
| TD-S06-7 | **Service principals are not modelled** | §14 question 2, answered at step 6: humans only for now. `TokenClaims.subject` and `AuditRecord.actor` stay `str` rather than becoming a sum of human and service identity, and the refresh lifetimes are human-shaped. Nothing in S01–S07 needs a service identity, and inventing the type ahead of a caller would be architecture nobody can validate. Revisit when the first non-human caller exists — the change is a domain type and a token lifetime, not a schema migration |
| TD-S06-8 | **A mistyped password can land in `audit_log.actor`** | A failed login records the subject as presented, which is what lets the log distinguish a brute force against one account from a scan across many. If an operator types their password into the username field, that password becomes a permanent audit row on a table nothing may edit. The mitigation is not obvious: hashing the actor destroys the grouping the field exists for, and the platform cannot tell a mistyped password from an unusual username. Recorded rather than solved, and it belongs with S42's threat-model work |
| TD-S06-9 | **Operator paths for principal administration, role assignment and bootstrap are deferred** | S06 stores and authenticates principals and administers role permissions, but deliberately does not create or assign principals outside tests. Registration, password reset, disablement, assignment and the idempotent first-authorised-principal bootstrap command have distinct authority and operational questions; they require their own approved slice rather than being inferred from permission grant/revoke |

Retention is *not* on this list: plan §12 already fixes it at indefinite and
immutable, so there is nothing deferred.

---

## 12. Proposed ADRs

| ADR | Title |
|---|---|
| **070** | Envelope encryption with per-record data keys behind a `KeyProvider` port |
| **071** | The audit log is append-only, enforced by the database, and publishes `AuditRecorded` through the outbox |
| **072** | Access tokens are short-lived and stateless; refresh tokens are stored, rotated on use, and revocable |
| **073** | Authorisation defaults to deny; order placement is a separate permission gated on enrolled 2FA |
| **074** | RLS policies are authored *and exercised* from the start, permissive in v1 |

No ADR is proposed for context placement or the boundary map, because §3
establishes that neither changes.

---

## 13. Delivery order once approved

1. ~~ADRs 070–074 recorded in `docs/adr/` and `docs/decisions.md`; checksums regenerated~~ — **done**
2. ~~Domain: value objects, `KeyProvider` and `TokenIssuer` ports, authorisation policy — no I/O, no framework~~ — **done**
3. ~~Migrations for the credential, audit and refresh-token tables, plus RLS policies~~ — **done**
4. ~~Envelope encryption adapter + credential store, against real PostgreSQL — and the G0 item from §2 closed~~ — **done**
5. ~~Append-only audit log, with tests proving `UPDATE` and `DELETE` fail, and `AuditRecorded` staged in the outbox~~ — **done**
6. ~~Token issuance, refresh rotation and reuse detection~~ — **done.** Also
   carries the `principal` table, which step 3 did not create: refresh tokens
   referenced an `account_id` and nothing named the human whose session it was,
   and ADR-072's "no such user versus wrong password are indistinguishable"
   presupposes a user store to be meaningful at all
7. ~~Authorisation~~ — **approved S06 scope done.** The owner approved exactly
   one tenant-scoped role per principal. Migration `0011`, the versioned `Role`
   aggregate, persistence, `RoleStore`, and the audited grant/revoke use cases
   are implemented and validated against real PostgreSQL. Tenant advisory
   locking, shared-role TOTP checks, optimistic concurrency and last-manager
   protection are enforced. Assignment writes, principal administration and the
   bootstrap operator command are explicitly deferred in TD-S06-9.
8. ~~RLS scaffolding, with a test that a restrictive policy bites~~ — **done.**
   Migration `0012` completes permissive policy enrollment; the Unit of Work
   sets transaction-local tenant context; completeness, isolation, rollback and
   pooled-reuse behavior are exercised against real PostgreSQL.
9. ~~Redaction coverage for S06 types; authentication metrics~~ — **done.**
   Both ADR-037 strategies are exercised with S06 material; login, refresh,
   grant and revoke outcomes use bounded labels and distinguish expected refusal
   from infrastructure error without accepting identity or secret data.
10. ~~Validation, documentation, v0.6.0~~ — **release candidate prepared.** The
    configured PostgreSQL/default suite, coverage, lint, format, strict typing,
    import contracts, custom boundaries, ADR guard, migration cycle, deployment
    dependency audit and release SBOM are recorded in the v0.6.0 release notes.

Steps 2–5 are the substance. If they are right, 6–9 are adapters and wiring.

---

## 14. Resolved questions and remaining decisions

Most of what I expected to ask is already decided: the plan fixes token
lifetimes (15 min), rotation, deny-by-default authorisation, 2FA, audit scope
and audit retention. The original three questions are now resolved.

1. ~~**New runtime dependencies.**~~ **Resolved:** `cryptography`, `pyjwt` and
   `argon2-cffi` are approved, pinned and present in every lockfile.
2. ~~**Who are the actors?**~~ **Answered at step 6: humans only for now.** The
   catalogue says JWT+refresh but not whether S06 authenticates humans only, or
   services too. Service-to-service authentication has different lifetimes and
   different revocation needs, and 15 minutes with a rotating refresh token is a
   human-shaped answer — which is the right answer for a single-operator tool
   before G-MCP. `TokenClaims.subject` and `AuditRecord.actor` stay `str`, and
   the deferral is recorded as TD-S06-7 so that adopting service identity later
   is a visible decision rather than a retrofit.
3. ~~**Does the audit log record credential reads?**~~ **Resolved:** credential
   reads are audited as `AuditAction.CREDENTIAL_READ`; this does not generalise
   into auditing every read in the platform.
4. ~~**May a principal hold more than one role?**~~ **Resolved 2026-08-02:** no.
   A principal holds at most one role, scoped to the same account. The composite
   principal/account foreign key makes orphaned and cross-tenant assignments
   unrepresentable; widening to multiple roles would require an explicit future
   migration and a policy for combining permissions.
