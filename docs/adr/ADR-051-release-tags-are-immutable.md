# ADR-051 — Release tags are immutable

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.6, section 4. Introduced by Amendment A17 after a near-miss during the S03 release.

## Context

ADR-029 established that every approved subsystem is tagged, but said nothing
about what happens if the tag is created before the work is finished.

During the S03 release it was created three times. The merge was tagged, then the
approval package landed, then the lessons document landed, and each time the tag
was moved forward with `git tag -f`. The final tag was correct.

It was correct **by luck**. The Product Owner's push happened to fall after the
last move. Had it fallen between two of them, the remote would hold `v0.3.0`
pointing at an earlier commit, a subsequent `git push origin v0.3.0` would have
been rejected, and resolving it would have required either a force-push over a
published tag or a hurried decision about which of two things `v0.3.0` means.

A release tag is a claim about what shipped. A claim that can be edited after
publication is not a claim.

## Decision

**Annotated release tags are immutable.** `git tag -f` and `git push --force` on
a tag are prohibited.

A release tag is created **only after every artefact is committed**: code,
documentation, ADRs, benchmarks, changelog entry, release notes, technical debt
register and all summaries. Tagging is the last act of a subsystem, not a step
within it.

If an error is discovered after tagging:

* make a corrective commit;
* if the error is material to what the release *is*, create a **new** tag —
  `v0.3.1`, not a moved `v0.3.0`;
* if it is not material, it rides in the next subsystem's release.

The previous tag stays exactly where it is, pointing at exactly what was
published, including its error.

This applies to release tags (`vX.Y.Z`) and gate tags (`gate-*`). Lightweight
scratch tags used locally are unaffected — they are notes to oneself, not claims.

## Rationale

The failure mode is not theoretical and was not hypothetical: it was one push
ordering away from occurring, in the first release where a tag was moved at all.

Immutability also makes the checksum-style reasoning behind ADR-027 apply to
releases. That decision made the ADR corpus trustworthy by making accepted
records unedittable; this makes releases trustworthy by the same mechanism. A
project that supersedes rather than edits its decisions should not rewrite its
release history either.

The alternative — allowing tag moves before the first push — was considered and
rejected. It depends on knowing whether a push has happened, which is exactly the
thing the author cannot reliably know in a workflow where someone else pushes.
A rule that holds only under a condition you cannot check is not a rule.

## Consequences

Tagging moves to the very end of the subsystem lifecycle, after the summaries.
The lifecycle ordering is updated to make this explicit.

Occasionally a subsystem will ship with a small documentation error preserved in
its tag. That is the intended trade: an accurate record of an imperfect release
beats a perfect record of an edited one.

A `vX.Y.1` patch tag becomes possible where previously the instinct was to move
the tag. That is a normal outcome, not a failure.
