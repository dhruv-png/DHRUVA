# ADR-072 — Access tokens are short-lived and stateless; refresh tokens are stored, rotated on use, and revocable

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S06 design document `docs/design/S06-identity-secrets-audit.md`, §7. Implements the AuthN row of plan §15.1.

## Context

Plan §15.1 already decides most of this: **JWT access (15 min) + rotating refresh
token**, over TLS with HSTS and secure, httpOnly, SameSite cookies. The S06
catalogue row adds **OIDC-ready**.

What the plan does not settle is the mechanism: where verification happens, what
revocation means when a token is verified by signature alone, and what "rotating"
obliges the store to remember.

There is a second token in this system and confusing the two would be the easiest
mistake in the subsystem. ADR-021's daily "Market Open Ritual" governs the **Kite
session token**, which S23 owns, which expires daily on Zerodha's schedule, and
which lives in the credential store of ADR-070. A user's refresh token and a
broker's access token have different lifetimes, different owners and different
failure modes.

## Decision

**Token issuance and verification sit behind a `TokenIssuer` port** declared in
`dhruva.contexts.platform.domain`. The adapter S06 ships signs and verifies JWTs
locally. An OIDC provider is a later adapter; no caller changes.

**Access tokens are stateless and live 15 minutes** (plan §15.1). They are
verified by signature and expiry alone, with no database lookup on the request
path. **They cannot be revoked before they expire.**

**Refresh tokens are rows.** Each is stored, belongs to an `account_id` (ADR-004),
records its lineage, and can be revoked.

**Refresh tokens rotate on use.** Presenting one issues a new refresh token and
invalidates the presented one. **Presenting an already-used refresh token revokes
its entire lineage** and is recorded as an audited authentication event
(ADR-071).

**Authentication failures are indistinguishable to the caller.** "No such user"
and "wrong password" produce the same error and the same code. The distinction is
recorded in the audit log, which is where it is useful.

Token expiry and revocation get codes in the closed error taxonomy (ADR-038),
alongside the existing `PermissionDeniedError` (`DHR-PRM-001`).

## Rationale

**Statelessness on the access path, state on the refresh path.** This is the
whole trade and it is worth naming. Checking a revocation list on every request
reintroduces exactly the database lookup the JWT existed to avoid, on the hottest
path in the system. Accepting that an access token cannot be withdrawn bounds the
damage by its 15-minute life instead — short enough that revocation of the
refresh token is the effective control, long enough that a user is not
re-authenticating constantly.

**Rotation with reuse detection.** A refresh token presented twice has one benign
explanation (a client retry after a lost response) and one serious one (a stolen
token being used alongside the legitimate client). The two are indistinguishable
at the moment of presentation, so the safe interpretation is the serious one:
revoking the lineage logs out an honest user occasionally and stops a thief every
time. Without stored lineage the detection is impossible, which is the second
reason refresh tokens are rows rather than signatures.

**A port rather than a JWT library at every call site.** "OIDC-ready" is a
commitment in the catalogue row, and it is only cheap if exactly one module knows
how a token is minted.

**Uniform authentication failures.** An error distinguishing an unknown user from
a wrong password is a user-enumeration oracle. It is useful to an attacker and to
nobody else; a legitimate user does not need to be told which half they got wrong.

Rejected: **stateful sessions in Redis** (every request becomes a network round
trip, and a Redis outage becomes a total authentication outage); **long-lived
access tokens without refresh** (compromise is unbounded); **non-rotating refresh
tokens** (theft is undetectable, and the plan says "rotating"); **a revocation
list checked per request** (the hot-path cost above, for a control the 15-minute
expiry already provides).

## Consequences

**A revoked user keeps working for up to 15 minutes.** This is the accepted cost
and it must be stated in the operator runbook, because it is precisely the
situation in which somebody will be watching and expecting immediacy. Where
immediacy is genuinely required, the mechanism is the kill switch (plan §15.1,
budgeted at under one second in §12 and owned by S25), not token revocation.

**New runtime dependencies.** A JWT library and a password hasher are not
currently dependencies. Both need provenance entries and a lockfile regeneration
on the canonical environment.

**Signing key management is not solved here.** The key is process configuration
(ADR-033), and rotating it invalidates every outstanding access token. That
interacts with ADR-070's master key custody and belongs with S42.

**Token verification sits on every authenticated request and has no performance
budget** (TD-S06-5). It needs one, from the first ADR-060 A1 Linux run.

**Clock skew becomes a correctness concern.** Expiry is evaluated against the
injected `Clock` (ADR-011), which keeps it testable, but a deployment whose clock
drifts will reject valid tokens or accept expired ones.
