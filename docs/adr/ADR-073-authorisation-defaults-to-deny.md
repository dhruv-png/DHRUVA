# ADR-073 — Authorisation defaults to deny; order placement is a separate permission gated on enrolled 2FA

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: S06 design document `docs/design/S06-identity-secrets-audit.md`, §8. Implements the AuthZ row of plan §15.1 and complements ADR-012 at the authorisation layer.

## Context

Plan §15.1 decides the policy: **role-based, defaulting to deny; order placement
is a separately granted permission**, and **TOTP 2FA mandatory for any account
with order permissions**.

ADR-012 makes the risk engine a structurally unbypassable pre-trade gate, so no
order reaches a broker without risk approval. That protects against a code path
that skips risk. It does not protect against an authenticated caller who was
never supposed to be able to place an order at all — a different failure, at a
different layer, needing a different control.

Amendment A2 requires explicit user confirmation on every order and a complete
audit trail, and makes broker approval a release blocker. Those obligations
assume the platform can say who was permitted to act.

## Decision

**Authorisation is role-based and denies by default.** An interface route with no
declared permission is **refused**, not permitted. This is enforced structurally:
a test enumerates the registered routes and fails if any lacks a declared
permission, so the default cannot be reached by forgetting.

**Order placement is its own permission.** It is never implied by an
administrative role, an "owner" role, or any notion of full access. A role
acquires it only by being granted it by name.

**The order permission cannot be granted to an account without enrolled TOTP.**
The check lives in the granting operation and fails it. 2FA enrolment is a
precondition of the permission, not a property of the user that some other code
path is expected to verify.

Granting, revoking and 2FA enrolment are **audited configuration changes**
(ADR-071).

Ordinary request authorisation is evaluated in the interfaces layer against
claims carried by the access token (ADR-072). Permission administration is the
narrow exception: grant and revoke use cases establish the actor's live tenant,
activity, TOTP enrolment and explicit `manage_authorisation` permission inside
the same application transaction that changes the role. This prevents a route,
worker or future command from bypassing the administrative policy. No domain
module authorises a caller, and no authorisation decision consults a position, a
balance, or any trading state.

## Rationale

**Deny by default, enforced by a test over the route table.** A default that
holds only when a developer remembers to declare a permission is not a default —
it is a convention with a security consequence. Enumerating routes turns "someone
forgot" into a build failure, which is the same instrument boundary rules R1–R9
already use.

**A separate order permission.** The most expensive authorisation mistake this
platform can make is an account that could place an order and was not meant to.
Bundling that capability into a broad role means the mistake is made by a
plausible-looking grant rather than by an obviously wrong one. This is the
authorisation-layer counterpart to ADR-012: the risk gate makes order placement
*unbypassable*, and this makes it *unimplied*.

**2FA as a precondition of the permission rather than a property of the user.**
The alternative — enrol 2FA, then separately remember to require it — is correct
until the first hurried afternoon, and it fails open. Making the grant itself
impossible without enrolment means the invariant cannot be violated by omission,
only by a deliberate change to this rule.

**Ordinary authorisation at interfaces; administrative invariants in their use
case.** Route permission checks remain at the interface boundary. A permission
mutation must also establish live management authority inside its application
transaction, because every adapter capable of invoking it must receive the same
policy and because manager eligibility can change after a token is issued. The
domain remains caller-agnostic and therefore replayable. Authorisation still
cannot depend on trading state, which is how "the position is small, allow it"
would otherwise become an authorisation rule.

Rejected: **attribute-based access control** (more expressive than a
single-operator v1 needs, and expressiveness here means more ways to write a
policy nobody can evaluate by reading); **permissive default with explicit
denials** (fails open, which for order placement means fails expensively);
**2FA at login only** (a session established before the permission was granted
would carry it).

## Consequences

**Every new route must declare a permission or the build fails.** This is
friction on purpose, and it will be felt most by whoever adds the first
route after this lands.

**A single-operator deployment carries multi-user machinery** it does not yet
need. The cost is accepted because plan §6 (S44) activates multi-tenancy later,
and retrofitting deny-by-default onto endpoints written without it is the
migration this avoids.

**2FA enrolment becomes a prerequisite for the platform's central function.**
Losing a TOTP device blocks order placement until recovery, and no recovery
procedure exists yet. That is an operational gap S06 should name and S42 should
close.

**ADR-012 and this ADR must not be conflated in review.** Two independent
controls guard order placement — unbypassable risk evaluation, and an unimplied
permission. Removing either because the other exists would leave a single point
of failure, and the compliance position in §2.2 assumes both.
