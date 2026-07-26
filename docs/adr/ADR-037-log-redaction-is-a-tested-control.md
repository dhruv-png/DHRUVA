# ADR-037 — Log redaction is a tested control with two independent strategies

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.4, section 4. Raised in the S02 design document and accepted on implementation.

## Context

ADR-020 requires credentials to stay out of logs and ADR-033 forbids them from
the repository, but neither says how a credential that reaches a log call is
stopped. The realistic leak paths are not malicious: a settings object gets
logged whole, an exception carries a connection string in its arguments, a
developer interpolates a token into a debug message at 6pm.

Redaction by field name -- the conventional approach -- misses every one of
those, because in each case the credential is not in a field called ``password``.

## Decision

Two independent strategies, applied by a single structlog processor.

**By key.** Any field whose name matches a deliberately broad sensitive pattern
is replaced wholesale, without traversal. A false positive costs one redacted
debug line; a false negative costs a credential rotation.

**By value.** Values wrapped in ``SecretValue`` register themselves in a
process-local set. The processor scans rendered strings, recursively through
dicts, lists, tuples and sets, and replaces any registered value it finds. A
minimum length of eight characters applies, because redacting every occurrence
of a four-character string would destroy ordinary log content.

The processor is placed **last in the chain, immediately before rendering**, so
it sees everything earlier processors merged in: bound context, formatted
exception text, stack info.

The registered-value snapshot is cached against a registry version counter.
Measured: rebuilding it per record cost roughly 40% of the total emission budget.

Verification is a **deliberate credential-leak test suite** that attacks the
control from every direction a real leak takes -- named fields, interpolated
messages, nested structures, exception arguments, bound context -- plus a test
asserting ordinary content survives untouched.

## Rationale

Either strategy alone is insufficient, and the failure modes are complementary
rather than overlapping: key-matching catches what is labelled, value-scanning
catches what is not. The cost of running both is measured at 1.02x, well inside
the 1.25x budget, which matters because a control that costs 3x will eventually
be switched off "just for this hot path".

Placing the processor last was tested against the alternative. A redactor
earlier in the chain misses exactly the cases that produce real leaks, since
those are the ones where the credential is merged in by something else.

## Consequences

Logging is the only sanctioned output path: ``print`` is banned by lint, and a
logger obtained directly from structlog would bypass the processor. That is why
``get_logger`` is the single accessor.

Value-scanning is best-effort and cannot see a transformed secret -- base64
encoded, truncated, embedded in a URL. This is recorded as a known limitation
rather than papered over; key-matching is the first net and this is the second.

The custom stderr logger factory exists because structlog's bundled one binds
its stream at construction, which makes the control impossible to capture in a
test. A security control whose output cannot be captured is a security control
nobody can verify.
