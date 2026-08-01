# S06 — Identity, Secrets Vault & Audit

> **Status:** Design, for review. No implementation until the ADRs in §12 are
> accepted, following the order S03, S04 and S05 each used.
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

---

## 9. RLS scaffolding (proposed **ADR-074**)

ADR-004 already decided it:

> Every domain table carries `account_id NOT NULL`. No global mutable state, no
> module-level caches keyed without account. **PostgreSQL RLS policies authored
> from the start, permissive in v1.**

**Finding: no RLS policy exists anywhere in the repository.** ADR-004 says
policies are authored *from the start*. As of this writing there is no
`CREATE POLICY` and no `ENABLE ROW LEVEL SECURITY` in any of the six migrations,
nor anywhere in `src` or `tests` — verified by search, not by memory. The
`account_id` half of ADR-004 was implemented in S04 and is enforced (
`daily_snapshot.account_id` is `NOT NULL`); the RLS half was not, and the gap has
been carried silently through v0.4.0 and v0.5.0.

This is an accepted ADR that is partly unimplemented, so closing it is not
optional and is not S44's job. It is not fixed here either, because the policy
expression and the name of the session variable the policies read are exactly
what ADR-074 must decide, and a migration cannot be verified in the sandbox
this design was written in. **S06 closes it at §13 step 8**, and that step should
be understood as repaying a debt rather than adding a feature.

"Permissive in v1" is the part needing care. A permissive policy that is never
exercised is a policy nobody knows works, and its first real test would be the
day it is switched on — which plan §6 schedules for S44, roughly forty
subsystems later. So the scaffolding must include a test that a **restrictive**
policy actually bites: set the session's account, read, and observe another
tenant's rows are unreachable. Plan §6 gives S42 an "RLS activation test
suite"; S06's job is to ensure that suite has something real to activate.

The session variable carrying the current account is set by the Unit of Work,
because the Unit of Work owns the transaction (ADR-053) and RLS is scoped to the
session. A caller setting it independently could set it in one transaction and
read in another.

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

---

## 11. Deliberately deferred debt

| ID | Item | Why deferred |
|---|---|---|
| TD-S06-1 | No external identity provider | The port exists; the adapter arrives when there is an operator population to federate |
| TD-S06-2 | RLS policies remain permissive | ADR-004 says v1 is permissive; enforcement is S44 |
| TD-S06-3 | Master key from the environment, not a KMS | ADR-020 permits either; an adapter swap |
| TD-S06-4 | No key-rotation job | The hierarchy makes rotation cheap; the job belongs to S42, which owns secret rotation |
| TD-S06-5 | **No performance budget for token verification** | It sits on every authenticated request and needs one. ADR-060 A1's Linux figures were unavailable when this was written; set the budget from the first E4 run |

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

1. ADRs 070–074 recorded in `docs/adr/` and `docs/decisions.md`; checksums regenerated
2. Domain: value objects, `KeyProvider` and `TokenIssuer` ports, authorisation policy — no I/O, no framework
3. Migrations for the credential, audit and refresh-token tables, plus RLS policies (ADR-055: reversibility, rollback and operational impact declared)
4. Envelope encryption adapter + credential store, against real PostgreSQL — **and the G0 item from §2 closed**
5. Append-only audit log, with tests proving `UPDATE` and `DELETE` fail, and `AuditRecorded` staged in the outbox
6. Token issuance, refresh rotation and reuse detection
7. Authorisation: deny-by-default, order permission, 2FA enrolment gate
8. RLS scaffolding, with a test that a restrictive policy bites
9. Redaction coverage for S06 types; authentication metrics
10. Validation, documentation, v0.6.0

Steps 2–5 are the substance. If they are right, 6–9 are adapters and wiring.

---

## 14. Open questions — I would rather you answer these than guess

Most of what I expected to ask is already decided: the plan fixes token
lifetimes (15 min), rotation, deny-by-default authorisation, 2FA, audit scope
and audit retention. Three things remain.

1. **New runtime dependencies, and they need your machine.** Neither
   `cryptography` nor a JWT library nor a password-hashing library is currently
   a dependency — I verified `pyproject.toml` and `requirements.lock`. S06 needs
   all three (conventionally: `cryptography` for AES-GCM, `pyjwt`, and
   `argon2-cffi`). Each needs a provenance entry that `test_toolchain_config.py`
   enforces, and all three lockfiles must be regenerated — which S05 established
   is work on the canonical Windows environment, not here. The exact commands,
   provenance entries and integration points are prepared in
   `docs/design/S06-dependency-plan.md`. **Please confirm the three libraries
   before ADR-070 and ADR-072 assume them.**
2. **Who are the actors?** The catalogue says JWT+refresh but not whether S06
   authenticates humans only, or services too. Service-to-service authentication
   has different lifetimes and different revocation needs, and 15 minutes with a
   rotating refresh token is a human-shaped answer.
3. **Does the audit log record credential *reads*?** Plan §15.1 enumerates four
   audited action classes and all are writes. Recording every read is defensible
   specifically for the credential store and unjustifiable everywhere else. I
   propose auditing credential reads only, and would rather have that confirmed
   than assumed.
