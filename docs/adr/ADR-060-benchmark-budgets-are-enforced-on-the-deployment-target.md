# ADR-060 — Benchmark budgets are enforced on the deployment target, not the development machine

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: proposed after the first complete canonical validation of S04
> (`docs/evidence/s04-20260729T151248Z/`), in which every functional stage passed
> and five performance budgets did not.

## Context

ADR-036 requires a performance budget to be declared before implementation and
changed only with a recorded reason. It did not say **where** a budget is
measured, because until S04 there was only one place to measure: a developer
sandbox. S04 introduced database-backed benchmarks, and with them a second
environment — and the two disagree by more than the budgets do.

The complete canonical run measured:

| Benchmark | Budget | E2 (Windows, Docker Desktop) | E3 (Linux, local socket) | E2 ÷ E3 |
|---|---|---|---|---|
| Database query p95 | 3 ms | 1.573 ms ✓ | 0.438 ms | 3.6× |
| End-to-end read p95 | 3 ms | **4.281 ms ✗** | 1.979 ms | 2.2× |
| End-to-end write p95 | 5 ms | **6.111 ms ✗** | 3.883 ms | 1.6× |
| Bulk append, 10,000 rows | 500 ms | 84.2 ms ✓ | 39.3 ms | 2.1× |

E2 is an 8-core, 24 GB laptop. E3 is a 2-vCPU, 3 GB **contended** sandbox. The
code is identical and the smaller machine is between 1.6 and 3.6 times faster on
every database measurement. The one number that admits no other reading is the
raw query: **1.573 ms for a single indexed primary-key lookup**. That is not the
cost of a query. It is the cost of a round trip through Docker Desktop's port
forwarding on Windows.

The CPU-bound benchmarks show the same pattern without any database at all:

| Benchmark | Budget | E2 (Windows, 3.12) | E1 (Linux, 3.10) | Over |
|---|---|---|---|---|
| `Money + Money` | 0.500 µs | 0.584 µs | 0.456 µs | +17% |
| `Money × int` | 0.500 µs | 0.625 µs | 0.455 µs | +25% |
| 2000-day range iteration | 1000 µs | 1062.9 µs | — | +6.3% |

`Money + Money` is 28% slower on newer Python and better hardware. Measured at
0.572, 0.582 and 0.584 µs across three runs, so this is a stable property of the
platform rather than noise.

DHRUVA is intended to deploy on Linux. Windows is a development convenience.

## Decision

**1. Linux CI is the authoritative environment for benchmark enforcement.** A
performance budget is met or missed based on a run in CI on Linux, against a
PostgreSQL reachable without a desktop virtualisation layer. That result gates
the build.

**2. Windows canonical runs record benchmark figures as informational.** They
continue to run, and their numbers continue to be recorded in
`PERFORMANCE_BASELINE.md` with their environment identifier. They do not gate a
merge and are not evidence that a budget is met or missed.

**3. Existing thresholds are unchanged.** Not one budget is relaxed by this
decision. `Money + Money` remains < 0.50 µs; the read p95 remains < 3 ms. They
are recorded as **unverified on the authoritative environment** rather than as
passing or failing.

**4. TD-13 and TD-14 are folded into this ADR.** Both are the same question in
older clothing — a budget measured somewhere other than where it applies. They
close when the Linux CI benchmark job produces a figure, not before.

**5. A benchmark that is not run in CI is not a budget.** Point 1 is only
meaningful if the job exists. Until it does, these budgets are undefended, and
that state is time-boxed by the acceptance criteria below.

## Rationale

The alternative was to optimise until the numbers pass on Windows. That is the
wrong move for a specific reason, not a convenient one.

The changes that buy milliseconds against Docker Desktop's network stack are
changes that reduce round trips: coarser sessions, longer-lived transactions,
caching between the repository and the database. Each of those is a real
architectural cost — held locks, staleness, a second source of truth — paid to
improve a number produced by a virtualisation layer that will not exist in
production. It would make the system worse on the machine that will run it, in
exchange for a green tick on the machine that will not.

The same argument applies to the microsecond budgets. `Money + Money` has already
been optimised once, from 1.856 µs to 0.456 µs, by removing eager guard
evaluation — a real defect with a real fix. There is no comparable finding
available at 0.584 µs. The remaining gap is interpreter and OS behaviour, and
chasing it would mean contorting the most-read arithmetic in the codebase for a
platform nobody will deploy on.

Recording a budget as *unverified* is also more honest than either alternative.
Relaxing it to 0.60 µs would assert that 0.60 is the right number, which no
measurement supports. Failing the build on it would assert that the code is too
slow, which no measurement supports either.

## Consequences

**A merge can proceed with informational benchmark failures.** S04 merges with
five budgets over on Windows, none verified on Linux. This is a deliberate
loosening of the gate and it is the main cost of this decision.

**`PERFORMANCE_BASELINE.md` gains an authority column in practice**: every figure
already carries an environment identifier, and from now on only E-Linux-CI
figures determine pass or fail.

**The canonical validation script keeps running benchmarks on Windows.** Removing
them would lose the cross-environment comparison that produced this ADR in the
first place. Its exit code continues to reflect them, so a Windows run may report
`OVERALL: FAIL` while the branch is still mergeable — the manifest states which
stage failed, and a reviewer must read it rather than the summary line.

**Two environments must be kept in the baseline, not one.** The 1.6–3.6× spread
is itself useful information: a future regression that appears on both is real,
and one that appears only on Windows is probably transport.

## Trade-offs

| Chosen | Rejected | Why |
|---|---|---|
| Enforce on Linux CI | Enforce on the developer's machine | The developer's machine is not the deployment target and its transport layer dominates the measurement |
| Keep thresholds, mark unverified | Relax thresholds to the Windows figures | No measurement supports 0.60 µs as a correct budget; it would encode a laptop's timer into the architecture |
| Keep thresholds, mark unverified | Optimise until Windows passes | The available optimisations trade real architectural properties for a virtualisation artefact |
| Keep running Windows benchmarks | Skip benchmarks off the authoritative environment | The cross-environment delta is diagnostic, and losing it would have hidden the finding that produced this ADR |

## Risks

**R-060-1 — This defers proof rather than providing it.** Today no environment
verifies these budgets. If the CI job is not built, the budgets quietly become
decoration and this ADR becomes the document that made that acceptable. This is
the principal risk and it is why acceptance criterion A1 is time-boxed.

**R-060-2 — A real regression could hide behind "it is only Windows".** The
phrase is now available as an excuse. Mitigation: the baseline keeps both
environments, and a regression that moves *both* is real regardless of which one
gates.

**R-060-3 — CI hardware is also not production hardware.** A GitHub-hosted runner
is not a trading server. This ADR moves enforcement closer to the target, not
onto it. Budgets remain approximations until measured on deployment hardware,
which is an S30-era concern and is not claimed to be solved here.

**R-060-4 — Benchmarks on shared CI runners are noisy.** Contended runners
produce false reds, and a gate that cries wolf gets ignored — which is how a
budget dies. Mitigation is in A2: the job must demonstrate stability before it
gates anything.

## Acceptance criteria

- **A1.** A Linux CI benchmark job exists and runs the `benchmark` marker against
  a PostgreSQL service container, **before S06 begins**. If S06 is reached
  without it, that is a stop-the-line item, not a backlog entry.
- **A2.** The job runs green on three consecutive commits before its result is
  allowed to gate a merge. Until then it reports and does not block.
- **A3.** Its figures are appended to `PERFORMANCE_BASELINE.md` under a new
  environment identifier, without editing E1, E2 or E3.
- **A4.** Every budget currently over on Windows is re-measured there, and each
  is then closed as met, or revised through a **new ADR** with the Linux figure
  as its evidence. This ADR revises no threshold.
- **A5.** TD-13 and TD-14 close, or are re-scoped with a Linux figure attached.
- **A6.** If a budget is genuinely missed on Linux CI, it is treated as a defect
  and optimised — this ADR is not a licence to miss budgets, only a statement
  about where missing one counts.
