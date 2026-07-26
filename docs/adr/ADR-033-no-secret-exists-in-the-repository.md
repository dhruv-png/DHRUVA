# ADR-033 — No secret exists in the repository; secrets arrive only from the configuration provider

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.3, section 4. Introduced by Amendment A10 on approval of S01.

## Context

This platform will hold a Zerodha API key and secret, daily access tokens, a
database password, and eventually credentials capable of placing orders. Every one
of those is a bearer credential: possession is authorisation.

A secret committed to git is not removed by deleting it. It remains in history, in
every clone, in every fork, and in any mirror — and rotating it is the only real
remedy. The cost of prevention is minutes; the cost of a leak is a rotation
exercise under time pressure, at best.

## Decision

No secret exists anywhere in the repository, at any commit, in any branch.

1. **Secrets come only from the configuration provider.** In practice: environment
   variables in development, the platform's secret store from S06, and a KMS or
   equivalent in production. No other source is sanctioned.
2. **`.env.example` only.** It contains variable *names* and obviously fake
   placeholder values. Real `.env` files are gitignored and never committed.
3. **Automated secret scanning runs in CI**, on every push and pull request and on
   a weekly schedule, over the **full history** rather than the diff — because a
   secret introduced and later deleted is still a leaked secret.
4. **Local prevention as well as detection.** `detect-private-key` runs in
   pre-commit, so the common case never reaches a remote at all.
5. **Secrets in memory use a non-disclosing type.** `SecretValue` never reveals
   itself through `repr`, `str`, formatting, logging or serialisation, and the only
   way to obtain the underlying string is an explicitly named method whose call
   sites are greppable.
6. **A test asserts the hygiene invariants**: no `.env` file is tracked, the
   ignore rules cover the patterns that matter, and `.env.example` contains no
   value that looks real.

## Rationale

Scanning history rather than the diff is the decision that matters most. Diff-only
scanning gives a green build for a repository that already contains a credential
in an old commit, which is precisely the situation a scanner exists to detect.

Layering prevention (pre-commit) with detection (CI) is deliberate. Prevention
alone fails when hooks are skipped; detection alone fails after the push has
already happened. Together they cover both.

`SecretValue` addresses the leak path that policy cannot: a developer logging an
object graph, or an exception whose arguments include a credential. Redaction by
key name misses these; a type that cannot render itself does not.

## Consequences

Debugging occasionally requires an explicit, visible `.reveal()` call rather than
printing a settings object. That friction is the feature.

CI gains a scan over full history, which is slower than a diff scan. Accepted.

If a secret is ever committed, the response is defined in advance: rotate the
credential first, purge history second, and treat the rotation as mandatory even
if the commit was never pushed.
