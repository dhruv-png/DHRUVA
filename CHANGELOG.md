# Changelog

All notable changes to D.H.R.U.V.A are recorded here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written for a reader who was not present. `git log` already holds the
commit messages; this file explains what changed and why it mattered (ADR-030).

**Version scheme.** A completed subsystem bumps the minor version and is tagged
`v0.<nn>.0`, where `<nn>` is the subsystem number. Gates are tagged `gate-<id>`.
`v1.0.0` is reserved for Gate G5 — the first release capable of touching live
capital (ADR-026, ADR-029).

## [Unreleased]

### Changed

- **AR-002 activates the private personal swing-assistant roadmap.** The
  institutional S07–S46 sequence is retained as history but indefinitely deferred
  as active work. Four product milestones now cover unified equity/futures/news
  data; scanning, backtesting and separate paper portfolios; news intelligence and
  optional validated ML ranking; and the private dashboard, alerts and operations.
- **Provider spending is bounded.** Read-only Zerodha market data is approved up to
  ₹500/month; news and sentiment/inference must have zero recurring cost. Current
  news uses official NSE sources, resilient public discovery/RSS and prospective
  point-in-time archiving. Sentiment runs locally with a deterministic fallback.

## [0.6.0] — 2026-08-02 — S06 Identity, Secrets Vault & Audit

S06 is complete. The credential store landed, and with it the encryption half of
the G0 checklist item that has been open since Phase 0. The audit log followed,
and it is the first table in the platform the application cannot edit. Then
tokens — which brought with them the platform's first application layer, and the
first table that says who a human is.

### Added

- **Bounded identity security metrics and S06 leak probes** (ADR-035,
  ADR-037). Authentication counts login and refresh outcomes; authorisation
  counts grant and revoke outcomes. Their only labels are closed operation and
  `succeeded`/`refused`/`error` enums, so subjects, tenants, roles, permissions,
  refusal reasons, credentials, hashes, tokens and TOTP material have no metric
  path. The deliberate leak suite now attacks S06 password, broker, access,
  refresh and TOTP material through both sensitive field names and interpolated
  registered values.
- **Exercised RLS scaffolding** (ADR-074). Migration `0012` enrolls the one
  tenant-owned table that predated the S06 policies, while the deployed v1
  predicates remain permissive. Account-scoped Units of Work set
  `dhruva.current_account_id` transaction-locally, and real-PostgreSQL tests run
  the restrictive form as a non-owner role to prove missing and cross-tenant
  contexts cannot see rows. A schema-completeness test keeps every non-null
  `account_id` table enrolled; nullable outbox provenance remains outside tenant
  ownership so unattributed system events are preserved.
- **Tenant-safe authorisation persistence** (ADR-073). A principal holds at most
  one role, and the assignment binds both principal identity and account so a
  real principal cannot borrow another tenant's authority. Roles retain the
  actor and instant behind each live permission, use optimistic versioning for
  concurrent changes, and round-trip through primitive records, pure mappers, a
  reconstruction factory and a Unit-of-Work-owned repository. Audited,
  tenant-scoped grant and revoke workflows enforce shared-role TOTP,
  self-escalation prevention, last-manager protection and optimistic
  concurrency. Principal administration, role assignment and the first-principal
  bootstrap operator path are explicitly deferred.
- **Authentication and session refresh** (ADR-072), as the repository's **first
  two application-layer use cases**. Every context's `application` package had
  been empty until now, so this also settles what a use case may depend on: ports
  in `domain.identity.ports`, never an adapter. Both run over fakes in the unit
  suite and over real PostgreSQL in the integration suite, unchanged.
- **A failed authentication commits.** The transaction writes its audit record
  and its outbox event, commits, and *then* the error is raised. That inverts the
  usual reading of ADR-053 and is the point: plan §15.1 requires a record for
  every authentication, and its own reasoning says why the failures matter — a
  run of them is the signal, and it exists only if it was written down. A
  rollback would leave the audit log showing successes only, which is the log
  that cannot show a brute-force attempt.
- **Refresh tokens rotate, and reuse revokes the lineage.** Presenting a spent
  token is treated as theft rather than as a retry (ADR-022's fail-closed posture
  applied to sessions), and the whole chain is revoked in one statement. The rule
  itself is `RefreshToken.verdict` in the domain, where its four cases can be
  enumerated without a database; the use case owns only the consequences.
- **Reuse and ordinary revocation are indistinguishable to the caller.** Both
  raise `TokenRevokedError` with the same message. Telling a thief they had been
  detected is the one thing worth withholding at that moment; the audit log
  records which it was. Expiry *does* get its own error, because the client's
  recovery genuinely differs.
- **`lineage_id` on `refresh_token`.** Every token in a rotation chain carries
  the same value, so revoking a compromised family is one predicate against one
  index. Derived from `parent_token_id` it would have been a recursive walk —
  slowest for the longest chain, which is both the most valuable to a thief and
  the one most likely to be under active abuse at the moment it runs.
- **The `principal` table** (migration `0010`), and with it the distinction
  between a **tenant** and **who acted**. `account_id` is what ADR-004 scopes
  rows to; `PrincipalId` is what an audit record names and a token asserts. They
  are one-to-one today, which is exactly why the separation had to be made in the
  schema now rather than discovered later.
- **`JwtTokenIssuer`** (HS256) and **`Argon2PasswordHasher`**, the only modules in
  the platform importing PyJWT and argon2-cffi. The issuer pins its algorithm
  list, so a token claiming `alg: none` is refused before its signature is
  considered — a test asserts it. Expiry is evaluated against the injected
  `Clock`, not PyJWT's wall-clock default (ADR-011), which is what makes it
  testable without sleeping and correct under replay.
- **`verify_nothing`**, a wasted argon2 verification on the unknown-subject path.
  ADR-072 requires "no such user" and "wrong password" to be indistinguishable;
  the same error is half of it, and the stopwatch is the other half. A lookup
  that misses returns in microseconds while a real verification spends deliberate
  milliseconds, which is a user-enumeration oracle no error message mentions.
- **`Sha256RefreshTokenMinter`.** SHA-256 here and argon2 for passwords, and the
  difference is what each algorithm is for: argon2's cost defeats a dictionary
  attack on a low-entropy secret, and a 256-bit CSPRNG token has no dictionary.
  Argon2 would have added ~100 ms to every refresh for nothing.
- **`PasswordHash` as a type rather than a `str`**, to make one specific
  catastrophe unrepresentable: a plaintext password in the field meant for its
  hash. As a bare string that is a silent, type-checking, test-passing mistake
  whose consequence is a database of plaintext passwords.
- **`UNATTRIBUTED_ACCOUNT`**, a reserved account for audit records that belong to
  no tenant. ADR-004 requires `account_id NOT NULL`; plan §15.1 requires auditing
  every authentication, including one presenting a subject no principal has. A
  nullable column was the obvious escape and the wrong one — it would make
  "unattributed" and "nobody filled this in" the same state on the one table that
  exists to be evidence.
- **Three error codes** — `AuthenticationError` `DHR-PRM-002`, `TokenExpiredError`
  `DHR-PRM-003`, `TokenRevokedError` `DHR-PRM-004` — and **`AuthSettings`**, whose
  placeholder signing key is refused in any deployed environment. A placeholder
  signing key is worse than a placeholder password: it needs no interaction with
  the platform at all, only the ability to read this repository.

- **The append-only audit log** (ADR-071, plan §15.1). `audit_log` refuses
  `UPDATE`, `DELETE` **and** `TRUNCATE` at the database, through a `BEFORE`
  trigger that raises rather than returning `NULL` — so an attempted deletion
  fails loudly instead of looking like a successful one. The trigger binds every
  role including the table owner, which is the only form of the guarantee that
  survives someone connecting with psql to "just fix one row". Three integration
  tests execute each of the three statements as raw SQL against real PostgreSQL,
  deliberately bypassing every application-level control, because a repository
  that offers no `update` method proves only that this repository offers no
  `update` method.
- **`TRUNCATE` is guarded, which ADR-071 does not require.** Row-level triggers
  do not fire on `TRUNCATE`, so a table guarded against only `UPDATE` and
  `DELETE` can still be emptied in one statement — the same erasure by a
  different verb, and the fast one. Plan §12 fixes retention at indefinite and
  immutable, which does not survive that. A statement-level trigger closes it.
  The consequence is accepted rather than worked around: no test may commit an
  audit row, and `audit_log` is deliberately absent from the integration
  harness's truncation list with a comment saying why.
- **`AuditRecorder`** — the one supported way to write an audit record. It writes
  the row *and* stages `AuditRecorded` into the outbox, in the caller's
  transaction, so neither can happen without the other. Both halves were already
  available separately, which was the problem: ADR-071 gives every audited
  subsystem an obligation, and an obligation discharged by remembering two calls
  is one that will eventually be discharged by remembering one.
- **`AuditRecorded`**, the Platform context's single published event (plan §5).
  It carries the record's fields rather than a pointer to them, so a consumer
  need not be given read access to the audit log to learn what happened. The
  identifiers are bare `UUID`s because a `SurrogateId` has no ADR-061 codec and
  could not have one that decoded back to the right subclass — carrying an
  identity that merely *looks* typed would be worse than carrying the value.
- **`AuditAction.CREDENTIAL_READ`** (ADR-071). The only audited read in the
  platform, and the exception is argued rather than assumed: "was this secret
  ever accessed, and by whom" cannot be answered retrospectively. The unit test
  pinning the action set is re-set rather than deleted, so a sixth still has to
  be argued.
- **The audit persistence layer** — `AuditLogModel`, `AuditLogRecord`, mappers,
  `AuditFactory` and an `AuditRepository` with no `update` and no `delete`. Their
  absence is not the guarantee and the module says so; the database is. The
  factory raises `DataQualityError` naming the row when a stored `action` or
  `outcome` is outside the domain enums, because on a table with no update path
  an unrecognised value would otherwise flow into a compliance export unmarked.
- **The credential store** (ADR-070, ADR-052, ADR-053). A `Credential` aggregate,
  a persistence record, an ORM model, a pure mapper, a reconstruction factory and
  a repository — S04's four layers, followed exactly, for the first table that
  holds something worth stealing. The repository is persistence and nothing else:
  it imports no cipher, takes no `KeyProvider`, and a read returns ciphertext.
- **`CredentialId`**, a minted domain identifier (ADR-009). Unlike the worked
  example's surrogate row key, this one is domain identity on purpose — the
  ciphertext is bound to it, and a binding to a value the domain refuses to know
  could not be recomputed on read.
- **`seal_credential` and `open_credential`.** The entire plaintext surface of
  the vault, in one small named module so that "where can a secret appear?" is a
  question with a greppable answer. `open_credential` returns a `SecretValue`,
  registered for redaction (ADR-037), so a credential that later reaches a log
  line is masked rather than printed.
- **Migration `0007_credential`** now carries `version`, and check constraints on
  both `version` and `key_version`.

### Changed

- **The HTTP dependency stack was upgraded without widening the application
  surface.** FastAPI is now `0.133.1`, Starlette is `1.3.1`, and Starlette's
  supported test client uses `httpx2==2.9.1`. The security workflow audits the
  hash-pinned deployment export rather than the editable development
  environment, and the v0.6.0 CycloneDX SBOM is generated from that same lock.

- **Envelope ciphertexts are bound to their record — TD-S06-6, closed.**
  `encrypt_secret` and `decrypt_secret` take `associated_data` as a **required
  keyword argument**. It is required rather than defaulted because a default of
  `b""` would make an unbound ciphertext the thing a caller gets by forgetting,
  which is the defect reintroduced as a convenience. The binding is the
  credential's identity, account and broker, in a versioned unambiguous
  encoding. An integration test moves one row's sealed values onto another with
  SQL and asserts the result no longer opens, while the untouched row still
  does. The data-key wrap is deliberately left unbound: binding it would widen
  the `KeyProvider` port and cost ADR-070's KMS-substitution argument, and buys
  nothing, since a wrapped key alone opens no credential.
- **`EncryptedSecret` moved from `infrastructure.crypto` to
  `domain.identity.credentials`**, and is re-exported from its old home. The
  credential aggregate has to hold it, and a domain class holding an
  infrastructure class is the import the layer contract exists to fail. The
  definition moved down a layer rather than the dependency being inverted around
  it.

### Fixed

- **`audit_log` disagreed with ADR-071 and with its own domain object**, in four
  ways at once. The table had `actor_id UUID` where the record has a string
  actor, a `resource_type`/`resource_id` pair where it has a single `subject`, a
  **nullable** `account_id` where ADR-004 and ADR-071 both require `NOT NULL`,
  and **no `correlation_id` at all** — the column ADR-071 names as what joins an
  audit entry to the log lines and events of the same request. It also carried a
  `metadata JSONB` catch-all: a free-form field on the one table that must never
  hold a secret, which is precisely what the domain module argues against at
  length and what ADR-037's redaction cannot help with once a value is stored
  deliberately. Corrected in migration `0008` in place, which had not been
  pushed or released. Correcting it rather than shipping an expand migration was
  the choice with less debt: expand would have left four columns nothing writes
  to, on the one table ADR-071 forbids dropping columns from.
- **`audit_log` and `refresh_token` had no SQLAlchemy models**, so
  `alembic --autogenerate` proposed dropping both tables — meaning the next
  migration generated for any unrelated change would have carried those `DROP`s
  into production. `AuditLogModel` and `RefreshTokenModel` now mirror their
  migrations column for column and index for index. `RefreshTokenModel` carries
  no behaviour and has no repository: it is schema metadata. Token issuance,
  rotation and reuse detection are now implemented and validated.
- **`credential` had no `version` column**, contradicting ADR-057's "every
  aggregate carries a `version` column". Found while writing the repository's
  `update`, which could not otherwise detect a lost update — two concurrent
  rotations would have silently left the row wrapped under a data key whose
  plaintext nobody recorded. Corrected in migration `0007` in place, which had
  not been committed or released.

## [0.5.0] — 2026-08-01 — S05 Event Bus & Job Runtime

S04 made events durable. This release makes them arrive — and makes a backtest
receive them the same way production does. The outbox now has a relay, a
transport, a dead-letter queue an operator can act on, a consumer idempotency
ledger and a job runtime; beside the live transport sits a replay adapter
reading the same rows, and both pass one unchanged conformance suite.

Trace propagation is deferred as TD-S05-16 with its reason recorded: the
consumer half has nowhere to attach an extracted context without a port
addition, which is an ADR-level change.

### Added

- **The Redis Streams adapter.** `RedisStreamPublisher` and `RedisEventStream`,
  the only code in the repository that knows what a stream id is. Deliberately
  thin: no retry, leasing, routing, ordering or dead-letter behaviour, all of
  which already live in the relay and in the domain policy.
- **A transport conformance suite.** Fourteen assertions written in the ports'
  vocabulary and nothing else, which every adapter must pass unchanged. ADR-067
  says live delivery and replay are peers; this is what makes that claim
  testable rather than aspirational. `InMemoryBus` is the second implementation
  that turns the suite from a description into a contract.
- **End-to-end coverage.** A fact committed by a use case, drained by the relay,
  and read by a consumer off a real Redis — the only tests that exercise the
  composition rather than the parts.
- **The consumer idempotency ledger** (ADR-065) and migration `0006`.
  `processed_event`, keyed `(consumer_group, event_id, run_id)`, written in the
  consumer's own transaction so a duplicate rolls the side effect back with it.
  Deliberately offers no `already_processed()` query: a check followed by a
  write is two statements with a gap, and two workers in one group can both
  pass the check before either writes.
- **Dead-letter inspection and explicit re-queue** (ADR-064), with the
  `dhruva-dlq` command. A published event and an event still being retried are
  both refused, each for a reason that would otherwise be discovered in
  production. Re-queueing without `--yes` is refused: the failure mode of a
  bulk re-queue is an outage.
- **Calendar-aware scheduling** (ADR-066). `TradingDaySchedule` declares a
  session-relative moment — `market_open + 5min`, `market_close - 15min` — and
  the calendar decides whether the day qualifies and what instant that is. A
  cron expression is wrong on every holiday and is a string no test can
  contradict; this is asserted against a weekend, a declared Monday holiday, a
  shortened session and Muhurat without waiting for any of them. Offsets are
  capped at a day, because beyond that the moment belongs to a different session
  than the one it names.
- **The Celery beat adapter.** `TradingDayBeatSchedule` answers beat's
  `is_due(last_run_at)` by delegating to the domain policy — no part of *when*
  lives in it, which is what makes ADR-066's "replacing Celery is an adapter
  swap" true rather than aspirational. The sleep is capped at five minutes so a
  beat holding a stale calendar re-evaluates; the next firing is sought after
  `last_run_at` rather than after now, so a restarted worker catches a run it
  slept through.
- **Replay as an `EventStream`** (ADR-069). `OutboxReplayStream` reads persisted
  events as a peer of the Redis adapter, and needed no migration: the relay
  already copies the outbox ordinal into the envelope and Redis stores it
  verbatim, so both transports report the same `sequence` by construction. The
  `as_of` bound is a constructor parameter enforced in the `WHERE` clause, so a
  replay is *incapable* of returning an event the platform had not yet learned —
  a filter a caller may forget is not protection. Acknowledgement writes nothing:
  a replay reads history and must not edit the record it is reading.
- **The Celery job runtime.** An application built from a settings slice rather
  than imported as a module-level global (ADR-031); a composition root sharing
  the API's one startup sequence, so worker records carry the same identity and
  the same redaction processor; `register_job`, which turns a plain function
  into a task so job modules import no Celery and are testable without a broker;
  and a declarative registry that refuses a duplicate name, a malformed name,
  and a scheduled job with no calendar to resolve it against.
- **A test that every platform error survives the worker boundary**, required by
  ADR-066 by name. The taxonomy is discovered by walking the subclass tree, so a
  new error class is covered the moment it exists rather than when someone
  remembers to add it to a list.
- **Redis in the integration harness**, with `DHRUVA_TEST_REDIS_URL` and
  `DHRUVA_REQUIRE_REDIS` mirroring the database variables. Integration tests are
  now marked from the fixtures they request, so a Redis test no longer demands a
  database it never touches.

### Fixed

- **The outbox wrote payloads with a different encoder than the relay reads
  them with.** A `Decimal` committed by a producer arrived at a consumer as a
  string: same characters, different type, no error anywhere. S05 had wired the
  envelope codecs into the read path only. Found by the first end-to-end test,
  because both sides were internally consistent and every isolated test passed.
- **`Celery.task()` leaks a job into every application built afterwards.** Its
  `shared=True` default appends a finalizer to a *module-level* list — the same
  mechanism `shared_task` uses — so a job registered on one application is
  re-created on every application finalized after it. That is the ambient global
  `set_as_current=False` was chosen to avoid, arriving through a different door.
  Found by the test asserting two applications do not share a registry.
- **The retry policy raised `OverflowError` at 1024 attempts.** Reachable: the
  count is read from a database column and a re-queued dead letter carries its
  attempts intact. Found by a property test on the policy's first day of having
  any unit tests at all.
- **`log.exception()` emitted a warning that failed an unrelated test.** The
  console renderer chose its exception formatter by probing for an installed
  package, so the log format depended on whether something had pulled `rich`
  into the environment (ADR-032).
- **The ADR corpus test asserted a frozen range** and had been red on this
  branch since ADR-060 was recorded. It now cross-checks the corpus against
  `docs/decisions.md`, which stays true as the corpus grows.
- **`__version__` was a release behind.** It read `0.3.0` while `v0.4.0` was
  tagged and released, so a deployed S04 build reported the previous version at
  `/health` and in its distribution metadata. Every gate stayed green: the
  existing assertions ask whether the version is *well-formed*, and all of them
  are true of `0.3.0`. A test now compares it against the newest released
  changelog heading — against the changelog rather than a git tag, so it fails
  in CI on the commit that forgets rather than later on whichever machine tags.
- **The deployment lockfiles had silently drifted from `pyproject.toml`.**
  `uv add` updates `uv.lock` only, so celery was declared, provenance-mapped and
  resolved — while `requirements.lock`, the hash-pinned artefact a deployment
  installs, did not mention it. The suite was green throughout: the existing
  check asserts only that hashes are present, which is true of a lockfile missing
  half its dependencies. It would have been found on the deployment target, by a
  worker that would not start. There is now a test that compares the two.
- **`mypy --strict` had twenty-two errors** in the relay's integration tests and
  had been red since that file landed. The whole tree checks clean.

### Changed

- **The conformance suite is scoped to the port.** Running it against replay
  failed one assertion — that a redelivery is preserved — and failed it for a
  correct reason: the outbox refuses a duplicate `event_id`, because it records
  what happened rather than what was delivered. Redelivery is what ADR-062
  promises of a *live* transport and what ADR-065's ledger absorbs; it is not a
  property of `EventStream`. The assertion moved to the Redis tests. The suite
  now asserts less, and what it asserts is true of every adapter rather than of
  most of them — the difference between a contract and a description of the
  first implementation.

- **Trace context is transport metadata, not domain state** (design §16b,
  decided by the Product Owner). `EventEnvelope` is unchanged. Putting a
  `traceparent` on it would make tracing part of the versioned wire contract and
  part of what `canonical_json` hashes — so two runs of the same replay, traced
  differently, would serialise differently and ADR-069's determinism would be
  gone. A field that changes an event's bytes depending on who was watching is
  not domain state.
- **`DateRangeLike` is now exported** from `dhruva.shared.time`. It is a
  parameter type in the `TradingCalendar` protocol and was not importable, so an
  implementer annotating the narrower `DateRange` violated contravariance and
  failed `mypy --strict`. Found by writing the first calendar implementation
  outside the package that defines the port — which is the only way that
  omission shows.

- **Explicit Redis stream ids were withdrawn** from the S05 design (§7 erratum).
  Redis requires an explicit entry id to exceed the top of the stream, and retry
  is per message while a stream is per aggregate type — so a retried sequence
  arrives behind later ones and is refused. De-duplication stays where ADR-065
  put it: the consumer's ledger, in the same transaction as the effect it
  guards. No accepted ADR asserted the withdrawn scheme.

## [0.4.0] — 2026-07-29 — S04 Persistence Foundation

> **Approved with the benchmark budgets deferred to ADR-060.** Five performance
> budgets are over on the Windows development environment and none is verified on
> the deployment target. No threshold was relaxed; they are recorded as
> *unverified*. ADR-060 makes the Linux CI benchmark job a stop-the-line item
> before S06.

The layer that turns domain objects into rows and back. Everything from here on
stores something, so the cost of getting this wrong compounds.

### Added

- **Async persistence stack.** SQLAlchemy 2 async engine, `UnitOfWork` owning the
  transaction (ADR-053), `DailySnapshotRepository` as the worked example, and a
  four-layer mapping — domain ← factory ← record ← model ← row — that keeps the
  domain persistence-ignorant (ADR-052).
- **Transactional outbox.** `OutboxWriter` stages events inside the caller's
  transaction, so an event and the change that caused it share one fate. The
  platform guarantees transactional durability, at-least-once delivery, a stable
  immutable `event_id`, per-aggregate ordering, and an idempotent consumer
  contract. The relay that reads the outbox arrives in S05.
- **The ORM-bypass write path (ADR-054).** `PostgresTimeSeriesStorage` appends
  allowlisted column rows with asyncpg `COPY`. No update, no delete, no lookup by
  identity — the interface enforces the exception rather than describing it.
  Boundary rule R8 fails the build if anything in that package imports a domain
  layer.
- **Migrations** `0001_initial` and `0002_example_tick`, each declaring its
  reversibility, rollback procedure and operational impact (ADR-055).
- **Optimistic concurrency** via a `version` column, surfacing a `ConflictError`
  that names the version it expected (ADR-057).
- **Integration suite against real PostgreSQL** (ADR-058) — 19 tests covering
  round trips, rollback, concurrent writers, isolation level, constraint
  enforcement and the timeseries path.
- **Nine benchmarks**, all implemented: five mapping-layer, four database-backed.

### Fixed

Three defects that only a real database could expose:

- **`Identifier` rejected the driver's own UUID.** The guard tested
  `type(value) is uuid.UUID`; asyncpg returns a *subclass*. Every identifier read
  back from PostgreSQL was refused, so the persistence layer could write rows it
  was structurally incapable of loading. A fake would have agreed with all 964
  unit tests. This is the single clearest justification for ADR-058 in the
  project so far.
- **`server_default` drift.** Models declared only the ORM-side `default` while
  the migration created DDL defaults, so `alembic revision --autogenerate`
  proposed dropping them — meaning the next migration written for any unrelated
  change would have carried that ALTER into production.
- **A fixture that waited on a lock it was itself holding**, truncating inside
  the session it was cleaning up after. No cycle for PostgreSQL to detect, so the
  suite hung forever instead of failing — and passed when the test ran alone.

### Decisions

ADR-052 through ADR-060. ADR-060 establishes that benchmark budgets are enforced
on the deployment target rather than the development machine, on the evidence
that every database figure is 1.6–3.6× slower on an 8-core Windows laptop than on
a contended 2-vCPU Linux sandbox.

## [0.3.0] — 2026-07-26 — S03 Domain Primitives & Shared Kernel

> **Approved with one documented exception.** Mutation testing (ADR-049) did not
> execute in the build environment and is tracked as **TD-12, HIGH, OPEN**. It
> must be run on the canonical Python 3.12 environment before G1, producing a
> real score and surviving-mutant analysis, with ADR-049 updated if the toolchain
> changes. The debt item closes only after successful verification.

The vocabulary every later subsystem speaks. Forty-three subsystems remain and
every one will import from here, so this is the layer where correctness is worth
the most and churn costs the most.

### Added

- **`Money`** — an exact integer count of minor units, never a float. Exact
  addition, lossless allocation by largest remainder, rate application with a
  named policy.
- **`Price`** — an integer at a **fixed 8-decimal scale**. This is the change
  that matters most: NSE currency derivatives tick at ₹0.0025, and a price at
  paise scale would have silently rounded every one of them, invisibly, until a
  position failed to reconcile.
- **`Quantity` / `SignedQuantity` / `Side`** — quantity is unsigned; direction
  never lives in a sign. `lots()` requires an explicit `lot_size`, because the
  gap between "3 lots" and "3 contracts" is a 25-fold position error.
- **`Ratio`** — one type with percent, basis-point and fraction constructors.
- **`RoundingPolicy`** — named for the market rule, not the mathematical mode.
  `STT_NEAREST_RUPEE` can be checked against a SEBI circular; `ROUND_HALF_UP`
  cannot. `CONSERVATIVE_TO_TRADER` exists because optimistic cost estimates are
  how a strategy appears profitable and is not.
- **`TradingDay` / `TradingCalendar` / `TradingSession`** — a trading day cannot
  be constructed without a calendar, and there is no `timedelta` arithmetic.
  NSE moved expiry from Thursday to Tuesday on 2025-09-01; a backtest spanning
  that date that computes expiry by date arithmetic is wrong and nothing flags it.
- **`Clock`** — injected, never read from the wall clock, forward-only when frozen.
- **`InstrumentId` / `AccountId`** — UUIDs we mint. A test walks the module AST
  and fails if any executable reference to a broker identifier appears.
- **`DomainEvent`** — facts, not commands. Transport metadata belongs to S05's
  envelope, so a fact means the same thing replayed as it did on the wire.
- **`DateRange` / `TimeRange`** — half-open, so consecutive ranges tile without
  double-counting a boundary.
- **Boundary rule R6** — no float under `shared/money`, in any of its three
  shapes, while permitting the `isinstance` guards that reject floats.
- **`docs/DOMAIN.md`** — 719 lines explaining *why*, with a domain map and twelve
  worked examples of financial bugs this design prevents.

### Changed

- Hot-path guards rewritten from `invariant(cond, msg, **ctx)` to explicit
  branches. The helper evaluates its arguments eagerly, so every addition was
  building an f-string and two `str()` calls before checking anything.
  **`Money + Money`: 1.856 µs → 0.456 µs. One million additions: 2.007 s →
  0.545 s.** Invisible to every gate except a measured budget.

### Decisions

ADR-042 (supersedes ADR-005) · ADR-043 · ADR-044 · ADR-045 · ADR-046 · ADR-047 ·
ADR-048 · ADR-049 · ADR-050. **The shared kernel is API-stable from this release.**

### Known limitations

Mutation testing did not execute and is recorded as TD-12 (HIGH, OPEN). Two
performance budgets miss marginally — `Money` construction by 25% and the
million-addition aggregate by 9% — both measured on Python 3.10 against a 3.12
target, both accepted as measured debt (TD-13, TD-14) rather than hidden.

### Engineering lessons

`docs/LESSONS.md` records what the mutation-harness investigation cost to learn:
property-test shrinking dominates mutation runtime, `ast.unparse` silently
discards comments, and a source-rewriting tool must be crash-safe by
construction rather than by cleanup discipline.

## [0.2.0] — 2026-07-26 — S02 Core Runtime

The platform's cross-cutting capabilities, and its first runtime component.

### Added

- **Typed, fail-fast configuration** (`dhruva.shared.config`). Validated once at a
  composition root and injected as typed slices. Deployed environments actively
  *reject* development-shaped values — debug enabled, placeholder credentials,
  human-readable log format — because valid configuration that is wrong is more
  dangerous than invalid configuration, which every other check would catch.
- **Boundary rule R5.** The build fails if any module outside `shared.config`
  reads `os.environ`, `os.getenv` or `dotenv`. Detects all six spellings. This is
  what makes ADR-010's shared execution kernel possible: a strategy cannot behave
  differently in backtest and live if it cannot see the environment.
- **`SecretValue`** — a credential that refuses to render itself through `str`,
  `repr`, f-strings, format specifications, `%`-formatting, JSON or pickle. Format
  specs are ignored rather than honoured, so `f"{secret:.4}"` cannot become a
  prefix oracle. Hashing raises: a hashable secret becomes a dict key, and dict
  keys end up in logs.
- **Structured logging with two-strategy redaction.** By field name *and* by
  registered value, applied last in the processor chain so it also catches
  secrets merged in from context and exception arguments — which is where real
  leaks come from. Verified by a deliberate credential-leak suite.
- **Closed error taxonomy** — eight families, stable `DHR-XXX-NNN` codes pinned by
  snapshot test, structured context as fields rather than interpolated text. The
  `SAF` family gives "we could not establish it was safe, so we stopped" its own
  identity, so the Risk Engine can distinguish a refusal from a failure.
- **Correlation context** — correlation, causation and account identifiers via
  `contextvars`, inherited by nested binds so end-to-end tracing survives layering.
- **Health, readiness and metrics registries**, and the `dhruva-api` component
  exposing `/health`, `/ready` and `/metrics`. Health performs no dependency
  checks by design: an endpoint that consults the database turns a slow database
  into a restart loop. Readiness fails closed — a check that cannot be evaluated
  counts as not ready.
- **Ordered `bootstrap()`.** Settings, then logging, then secret registration,
  then tracing, then a banner stating whether tracing is actually active. The
  ordering is the design: reversing steps two and three would leave a window in
  which a credential could be logged in the clear.
- **Reproducible-build artefacts** — hash-pinned lockfiles, `docs/BUILD.md`, and a
  test asserting the three Python version declarations agree.
- **Performance budget suite** — six measured budgets, run separately from the
  inner loop.

### Changed

- `bind_correlation` rewritten from a `@contextmanager` generator to a class-based
  context manager: **17.7 µs → 2.0 µs** per call. It sits on the hot path of every
  request and every tick batch.
- The log redactor now caches its secret snapshot against a registry version
  counter instead of rebuilding two sets per record: **71.9 µs → 36.7 µs** per
  emission.

Neither optimisation would have been found without ADR-036's requirement to
measure before implementing.

### Decisions

ADR-031 (configuration placement), ADR-032 (reproducible builds), ADR-033
(secrets), ADR-034 (release governance), ADR-035 (observability first), ADR-036
(performance budgets), ADR-037 (redaction as a tested control), ADR-038 (error
taxonomy), ADR-039 (correlation), ADR-040 (OpenTelemetry split), ADR-041
(technical debt register).

### Known limitations

Eight open and three accepted items in the S02 technical debt register. The two
`HIGH` items — canonical `uv.lock`, and validation on Python 3.12 — share a single
trigger: access to a 3.12 host.

## [0.1.0] — 2026-07-26 — S01 Repository, Tooling & CI Skeleton

First subsystem. Establishes the foundation every later subsystem is built on,
and — more importantly — turns the approved architecture into controls that fail
the build when it stops holding.

### Added

- **Monorepo skeleton.** Six areas: `backend`, `frontend`, `infra`, `docs`,
  `research`, `.github`. Nine bounded-context packages, each with four Clean
  Architecture layers and a single public `api` module. Three composition roots.
- **`dhruva.tooling.boundaries`** — an AST-based checker enforcing four
  architectural rules: public-API-only access between contexts, the declared
  dependency matrix from plan §5, no inward dependency on composition roots, and
  a leaf-only shared kernel. Resolves relative, star and plain-`import` forms, so
  the rules cannot be evaded by changing import style.
- **`dhruva.tooling.adr_guard`** — decision-log integrity and immutability
  (ADR-027). SHA-256 of each record's body, excluding only the two lines that
  legitimately change. `--update` registers new records and refuses to overwrite
  an existing one; that refusal *is* the enforcement mechanism.
- **Toolchain**: uv, ruff (with `DTZ`, `T20`, `FIX`, `ERA`, `S`, `ASYNC`, `D`
  rule families selected against specific plan decisions), `mypy --strict`,
  pytest, import-linter, pre-commit.
- **CI and security pipelines.** Gates ordered cheapest-first so a style error
  fails in seconds rather than after the test suite. Weekly dependency, secret and
  container scans.
- **Local data stack.** PostgreSQL 16 with TimescaleDB, Redis 7.4, both with
  health checks. Container timezone pinned to UTC so any accidental local-time
  dependency fails loudly in development rather than silently in production.
- **28 Architecture Decision Records**, generated from the approved plan and
  checksum-registered.
- **162 tests, 100% statement and branch coverage** of `dhruva`. Each control is
  tested twice: against the real codebase, and against synthetic trees that
  deliberately break it.

### Notable decisions

- Context reachability is enforced by a purpose-built checker rather than by
  import-linter, which cannot express "only via `B.api`" without 72 hand-written
  contracts that would rot on the first rename. Layer ordering stays with
  import-linter, which expresses it natively.
- Docker Compose ships data services only. Four application containers with
  nothing to serve would be placeholders, and the Golden Rules forbid those.
- Runtime dependencies are empty, and a test asserts it — a dependency appearing
  there means a subsystem was started without a design document justifying it.

### Known limitations

Documented in full in `docs/subsystems/S01-repository-tooling-ci.md`:

- The boundary checker analyses imports, not calls. Runtime indirection —
  `importlib`, a registry, a string-keyed factory — is invisible to it.
- `ALLOWED_CONTEXT_DEPENDENCIES` is a transcription of plan §5. If it drifts, the
  checker will enforce the wrong architecture perfectly.
- The ADR guard hashes bodies, not meaning. A new ADR that contradicts an old one
  without superseding it passes every check.

[Unreleased]: https://github.com/dhruv-png/DHRUVA/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.3.0
[0.2.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.2.0
[0.1.0]: https://github.com/dhruv-png/DHRUVA/releases/tag/v0.1.0
