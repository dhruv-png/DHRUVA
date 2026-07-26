# ADR-038 — Errors are a closed taxonomy with stable machine-readable codes

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.4, section 4. Raised in the S02 design document and accepted on implementation.

## Context

Errors in this platform cross more boundaries than usual: they surface in logs,
in API responses, in the explainability payload a user reads, and eventually in
alert rules. An exception type alone carries none of that -- callers end up
matching on class names they do not own, or on message substrings.

## Decision

Eight families, closed. Every error derives from ``DhruvaError`` and carries:

* a **stable code** of the form ``DHR-XXX-NNN``, validated at construction and
  pinned by a snapshot test;
* **structured context as fields**, never interpolated into the message;
* a **retryability flag**, so callers do not infer it from the class name.

A new family requires an ADR; a new member within a family is ordinary work.

``repr`` exposes context *keys* but never values, because ``repr`` is called
implicitly by debuggers and some logging paths, and a context value can be a
credential. ``to_dict`` is the deliberate, greppable way to obtain values.

The ``SAF`` (safety) family is its own branch, distinct from failure.

## Rationale

Stable codes are what make an alert rule survive a refactor. Renaming a class is
routine; changing the meaning of ``DHR-DQL-002`` is not, and the pinning test
makes the difference visible in review.

Structured context rather than interpolation serves two ends at once: the fields
are queryable, and the message cannot accidentally carry a credential -- which is
the same reasoning as ADR-037, applied one layer earlier.

The safety family exists because ADR-022 makes ambiguity fail closed, and a
system that reports "we could not establish it was safe, so we stopped" as a
generic error teaches its operator to ignore it. The Risk Engine (S25) will
branch on this distinction, and the interface will report it honestly.

## Consequences

Adding an error means adding a code and updating the pinned snapshot in the same
commit. That is friction, and it is the point.

Errors must remain picklable, because Celery serialises exceptions across process
boundaries from S05. An error that cannot cross that boundary turns a useful
failure into an opaque one.
