# ADR-077 — Bind a credential to its purpose, not just its broker

- **Status:** Accepted
- **Date:** 2026-08-07
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

ADR-070 gave every broker credential one encrypted row, and TD-S06-6 bound that
row's ciphertext to `(credential_id, account_id, broker)` so ciphertext lifted
from one record into another fails to decrypt. The `credential` table enforces
`UNIQUE (account_id, broker)`, and the domain docstring states the rule plainly:
one credential per broker per account.

Wiring Zerodha broke that assumption. Kite Connect requires two secrets whose
lifecycles have nothing in common:

- **API key and API secret.** Issued by the broker to the owner when the Kite
  Connect application is created. Long-lived, rotated when the owner chooses,
  and a rotation is a security event somebody should notice.
- **Access token.** Minted by a browser-mediated login and exchanged from a
  short-lived request token. It expires on the broker's schedule — for Kite,
  the following morning — and is replaced as a matter of daily routine.

One row cannot hold both honestly. `rotated_at` exists to answer "when did this
secret last change"; if a daily login re-seals the row, that column answers "when
did I last log in" instead, and the question it was added for becomes
unanswerable. `version` and the optimistic-concurrency rules (ADR-057) have the
same problem: a routine login and a deliberate secret rotation become the same
kind of write.

The schema also had no way to say which of the two a row held, so nothing could
look up "the session for this broker" without also matching whatever else was
stored under it.

## Decision

Credentials gain a closed **purpose**, and the purpose participates in identity,
uniqueness and the cryptographic binding.

1. `CredentialPurpose` is a domain `StrEnum` with exactly two members,
   `ENROLMENT` and `SESSION`. It is generic: these are properties of secrets,
   not of Zerodha.
2. `Credential` carries `purpose`, and refuses to construct without a member of
   that enum.
3. The AES-GCM associated data becomes
   `(scheme, credential_id, account_id, broker, purpose)`. The scheme tag moves
   from `dhruva.credential.aad.v1` to `…v2`.
4. Migration `0017_credential_purpose` adds `purpose VARCHAR(16) NOT NULL` with
   `CHECK (purpose IN ('ENROLMENT','SESSION'))`, and replaces
   `UNIQUE (account_id, broker)` with `UNIQUE (account_id, broker, purpose)`.
5. `CredentialRepository.get` takes a purpose. There is no lookup that omits it.
6. `broker` keeps meaning the broker. It is not overloaded to carry a purpose.

Session **expiry** is deliberately not added to the credential aggregate. This
decision separates secret lifecycles; it does not attempt to design the broker
session model, which belongs with the login use case that will produce one.

## Rationale

**Overloading `broker` as `zerodha-session` was rejected.** It requires no
migration, which is its only merit. `broker` is bound cryptographically and is
documented as naming the broker; making it sometimes name a credential kind
means `uq_credential_account_broker` silently stops meaning what the domain says
it means. A schema whose semantics changed with nothing recording the change is
worse than a migration, not better — the migration is at least reviewable.

**One sealed document holding both was rejected.** It also avoids a migration,
and it destroys the distinction that motivated the work: every login re-seals the
API secret, moves `rotated_at`, and increments `version`. The two secrets would
share a rotation history, a concurrency token and a blast radius. Losing the
session would mean rewriting the enrolment material, and a bug in session
handling could corrupt the owner's long-lived secret.

**Purpose is a closed enum while `broker` stays a string.** ADR-003 keeps the
platform provider-agnostic and no decision enumerates the supported brokers, so
`broker` remains honestly open. Purpose is the opposite: this ADR fixes exactly
two meanings, and the entire value of the distinction is that the two never
blur. An open string would let a third meaning appear without a decision, and let
two spellings of one meaning produce two bindings.

**The binding must include purpose, or the separation is cosmetic.** Without it,
a session ciphertext copied into the enrolment row decrypts cleanly, and the
system reads a token that expires tomorrow as the owner's long-lived secret. The
same construction TD-S06-6 used against cross-row theft is the one that has to
cover cross-purpose theft; the alternative is two rows a cipher cannot tell
apart.

**The scheme tag moved to v2 rather than widening v1 silently.** A widened v1
would leave old ciphertext openable under a construction that had stopped
distinguishing the two lifecycles — the precise confusion this record exists to
prevent. Failing closed is correct even though, today, it costs nothing.

## Consequences

**Easier.** A broker session can be replaced daily without touching enrolment
material. "When was my API secret last rotated" and "when did I last log in" are
different columns on different rows. A missing session is a missing row, which
is a clean, explicit state for the login command to report. Any future broker
needing both lifecycles inherits the model.

**Harder.** Every credential read must state a purpose; there is no
purpose-agnostic lookup, deliberately. Callers gain an argument.

**Migration and rollback.** Upgrade is additive and touches no ciphertext.
Downgrade **refuses** when any account holds more than one purpose for a broker,
because collapsing the uniqueness would require deleting a row whose sealed
material cannot be regenerated. A recoverable failure is better than a silent
loss; the operator deletes the unwanted credential explicitly and retries.

**Existing rows.** There are none. No composition root writes a credential —
no enrolment use case exists and no CLI reaches the store — so every credential
row that has ever existed lived inside a test transaction. The backfill is
therefore unconditional and cannot misclassify anything, and the `server_default`
is dropped immediately so future inserts must state a purpose rather than
inherit one. Had ambiguous rows existed, a migration would have been the wrong
place to resolve them: re-sealing under a new binding needs the master key, which
a migration does not have and should not.

**Audit.** Records naming a credential continue to name it by `credential_id`,
which remains unique. What changes is that two credentials for one broker are now
distinguishable in an audit trail rather than indistinguishable.

**Not enabled by this.** This record is the foundation for broker
authentication. It stores no Zerodha material, opens no session, makes no
network call and fetches no market data.
