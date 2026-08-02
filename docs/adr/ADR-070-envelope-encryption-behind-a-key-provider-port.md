# ADR-070 — Envelope encryption with per-record data keys behind a KeyProvider port

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S06 design document `docs/design/S06-identity-secrets-audit.md`, §4. Implements ADR-020, which fixed the requirement in Phase 0; nothing here replaces it.

## Context

ADR-020 requires that the Kite API secret and access tokens be envelope-encrypted
with a per-record data key and a master key from environment or KMS. Plan §15.1
repeats it as a security control. Neither says which cipher, where the key
hierarchy lives, or what the code holds.

Nothing in `backend/src/dhruva` encrypts anything today, because no subsystem
before S06 stores a secret. That makes G0's checklist item — *"secrets encrypted
at rest"* — open, and it makes this the first and cheapest moment to fix the
shape, since there is no stored ciphertext to migrate.

The forcing constraint is that S23 cannot begin without somewhere for a broker
credential to live, and S07 depends on S06 for the same reason.

## Decision

A **two-level key hierarchy**. Each stored credential is encrypted with its own
freshly generated **data key**. The data key is encrypted ("wrapped") with the
**master key**. The row stores the ciphertext and the wrapped data key, and
stores neither the master key nor any plaintext data key.

The master key arrives from the configuration provider as a `SecretValue`
(ADR-033, boundary rule R5). It is **process** configuration and lives in
`dhruva.shared.config`, not in the Platform context's persisted configuration
(ADR-031). The key that decrypts the vault is never stored in the vault.

Wrapping and unwrapping sit behind a **`KeyProvider` port** declared in
`dhruva.contexts.platform.domain`. S06 ships one adapter, backed by the
configured master key. A KMS-backed adapter is a later substitution requiring no
change to the domain or to any repository.

**Decryption is explicit.** The credential repository's read returns ciphertext.
Obtaining a plaintext requires a separate call that takes the `KeyProvider` as an
argument. There is no code path that yields a plaintext without one.

The cipher is **AES-256-GCM**, providing authenticated encryption, with a unique
nonce per encryption operation.

## Rationale

**Per-record data keys rather than encrypting each credential directly with the
master key.** Direct encryption makes the master key's compromise total, and
makes rotating it an operation that must decrypt and re-encrypt every credential
— a migration, with downtime and a half-migrated state to reason about. With a
hierarchy, rotation re-wraps N small data keys and never touches a ciphertext.
Plan §6 assigns secret rotation to S42, so this cost will be paid; the hierarchy
decides whether it is paid in an afternoon or a maintenance window.

**A port rather than calling a crypto library from the repository.** ADR-020
writes "environment/KMS", so both are already sanctioned and the choice will
change. A repository that imports a KMS SDK cannot be unit-tested without one,
and a domain that knows which one it has is a domain coupled to an operational
decision.

**Authenticated encryption rather than encrypt-only.** Without authentication, a
modified ciphertext decrypts to garbage that the application then treats as a
credential. GCM makes tampering a decryption failure, which is an error the
error taxonomy can name.

**Explicit decryption rather than a repository that returns plaintexts.** A
plaintext returned by default is a plaintext that reaches a log line, an
exception message, or a debugger by accident. Requiring an argument makes the
plaintext path greppable, which is the same reasoning ADR-033 applied to
`SecretValue`'s single named accessor.

Rejected: **application-level encryption of the whole table** (rotation and
per-record blast radius both become untestable); **PostgreSQL `pgcrypto`** (the
key would travel in SQL statements and reach the query log, and the database
would hold both ciphertext and the means to read it); **full-disk encryption
alone** (plan §15.1 lists it separately, and it protects a stolen disk, not a
compromised connection).

## Consequences

**A new runtime dependency.** `cryptography` is not currently a dependency. It
requires a provenance entry that `test_toolchain_config.py` enforces and a
regeneration of all four lockfiles with the canonical `uv pip compile` commands.

**Losing the master key destroys every stored credential**, irreversibly and by
design. That obliges an operational key-custody procedure, which does not exist
yet and belongs with S42's secret rotation work.

**Every credential read costs an unwrap.** The credential store is not on a hot
path today, but it will be once S23 reads a broker token per session, and this
ADR fixes no budget for it. TD-S06-5 carries that.

**The G0 checklist item closes** when the credential store lands, and S06's
definition of done should say so explicitly rather than leaving it to be noticed.

**Existing ciphertext is not a concern**, uniquely, because there is none.
Deciding this after S23 stored its first token would have made it a migration.
