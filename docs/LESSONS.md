# Engineering Journal

A **permanent, accumulating record** of what building this platform actually
taught — not a release artefact, and not a summary. It grows with every
subsystem and is never pruned.

Findings kept here cost time to discover and would cost the same time again. The
next person to hit one of these is, on a project with a bus factor of one, most
likely the author having forgotten (risk R15).

**What belongs here:** engineering discoveries · tooling limitations · failed
approaches · benchmark findings · debugging techniques · estimation accuracy ·
architectural trade-offs and how they aged.

**What does not:** anything already stated as a decision (that is an ADR), and
anything already stated as a deferral (that is the technical debt register).

Organised newest-subsystem-first. Each entry states what happened, why, and what
to do instead.

---

## S04 — What the first real database run found

Six defects, found the first time the integration suite ever executed against a
live PostgreSQL. Every one of them was invisible to 937 passing unit tests.

### An exact type check rejects the driver's own UUID

``Identifier.__init__`` tested ``type(value) is not uuid.UUID``. asyncpg does not
return ``uuid.UUID``; it returns ``asyncpg.pgproto.pgproto.UUID``, a **subclass**.
So every identifier read back from PostgreSQL was rejected, and the persistence
layer could write rows it was structurally incapable of loading. The round trip
was broken in one direction only, which is the direction no unit test looks at:
construct a ``uuid.UUID`` in Python and the exact check passes every time.

This is ADR-058 -- real PostgreSQL, no SQLite substitute -- earning its cost in a
single defect. A fake would have handed back a plain ``uuid.UUID`` and agreed
with the unit tests all the way to production.

**What to do.** ``isinstance`` for the guard, and normalise to the plain type on
the way in. ``isinstance`` loses nothing here -- ``str`` is not a UUID subclass,
so the ``InstrumentId("NIFTY")`` case the guard exists for is still refused --
and normalising keeps a driver type from taking up residence inside the domain
model, where an identifier's class would otherwise depend on whether it was
constructed or loaded.

**Worth generalising:** an exact type check is a claim about who is allowed to
construct your inputs. At a system boundary, you are not the one constructing
them.

### A cleanup nested inside the thing it cleans up waits for itself

``committed_session`` ran ``TRUNCATE`` in a ``finally`` block *inside*
``async with factory() as bound``. A test finishing on a read left that session
idle in transaction holding ACCESS SHARE; TRUNCATE wants ACCESS EXCLUSIVE. There
is no cycle for PostgreSQL to detect -- nobody is going to ask the first session
for its lock, because the code that would close it is waiting on the TRUNCATE --
so the suite **hung forever instead of failing**.

It passed when that test ran alone, because alone the session's last act was a
commit and it held nothing. Run in sequence, it hung. A test that passes in
isolation and hangs in company is describing its fixture, not its subject.

**What to do.** Release the resource, then clean up after it -- ordering, not
nesting. And put a ``lock_timeout`` on any statement that takes an exclusive
lock in a test: an infinite hang carries no information, while a five-second
failure naming the table is a diagnosis.

### A test that commits must own its cleanup, and must be able to

``test_deadlock_surfaces_as_a_driver_error`` commits two rows and took ``migrated``
directly, so the truncation welded inside ``committed_session`` never ran for it.
The next test's first insert then collided with ``uq_daily_snapshot`` and failed
for a reason with nothing to do with what it asserts.

The rule was written down -- "tests that observe committed state clean up after
themselves explicitly" -- and could not be followed, because the only cleanup
available came bundled with a session fixture this test did not want.

**What to do.** When a rule is stated in a docstring, check there is a mechanism
for obeying it that does not require taking something unrelated. The cleanup is
now its own fixture, so any committing test can opt in on its own terms.

### asyncio.run() cannot be called from inside a running loop

``alembic/env.py`` drives its async migration with ``asyncio.run()``. The
``migrated`` fixture is async, so the session loop was already running, and every
test in the suite errored at setup with
``RuntimeError: asyncio.run() cannot be called from a running event loop``.

**What to do.** ``await asyncio.to_thread(...)``. The migration tool owns how it
manages its own loop; the harness should give it the bare thread it expects
rather than reach inside and restructure it.

### A marker added during collection arrives too late

The asyncio marker was synthesised in ``pytest_collection_modifyitems``. By then
pytest-asyncio has already decided how to invoke each item, so the marker was
reported as ``"marked with '@pytest.mark.asyncio' but it is not an async
function"`` -- which, under ``filterwarnings = ["error"]``, failed all twelve
tests. The earlier symptom (async tests silently not running at all) was real;
the fix was applied at the wrong point in the lifecycle.

**What to do.** Declare it in the module's ``pytestmark``, where the plugin can
see it during collection. The collection hook now *verifies* the marker is
present and refuses to collect without it, which keeps the guarantee -- an async
test can never be silently skipped -- without fighting the plugin for control of
it.

### A deprecation warning can be a total outage

Alembic 1.16 warns when ``path_separator`` is absent from ``alembic.ini``. With
``filterwarnings = ["error"]``, that warning was raised inside the ``migrated``
fixture and every database-backed test errored during setup. A note about how a
config key will be parsed in a future release presented as a complete failure of
the persistence suite.

**What to do.** ``filterwarnings = ["error"]`` is right and stays. The corollary
is that a dependency's deprecation warning is a build break on a timer: address
them when the dependency is upgraded, not when they detonate.

---

## S04 — The validation harness

Six defects in the harness, found across two attempts by someone else trying to
run it. None were in the code under test. All were mine.

### A skip is not a result, and a log full of skips reads like a log full of work

The integration gate was `skipif(not os.environ.get(DATABASE_URL_ENV))`,
evaluated at import. The `database_url` fixture had a testcontainers fallback for
machines with Docker — which could never run, because the skip fired first. The
skip reason said "or run with Docker available so the container fixture can start
one." That sentence was false for the entire life of the file.

The consequence was worse than the bug. Twelve `SKIPPED` lines appeared in the
evidence and were reported back as "integration stages execute." They executed
nothing. A green-looking run that asserts nothing is more expensive than a red
one, because it ends the investigation.

**What to do.** Two things, and the second matters more:

1. A capability gate must test the capability, not a proxy for it. Docker is
   available when the daemon answers a ping — not when an environment variable
   is set, and not when the library imports.
2. Give the harness a mode in which skipping is a hard error
   (`DHRUVA_REQUIRE_DATABASE=1`), and have the canonical runner set it. In a run
   whose only purpose is to produce database-backed evidence, "no database" is a
   failure, not a reason to pass quietly.

### Provisioning that lives inside pytest cannot serve stages outside it

Migration stages 30–35 invoke `alembic` as standalone processes. A testcontainers
database exists only inside the pytest process, for the lifetime of the session.
So a run without an explicit URL reached those stages with no database in
existence, fell through to the settings defaults, and failed authenticating as
user `dhruva` — six identical stack traces, none of which mentioned the actual
cause.

Making the parameter optional was the error. The script advertised a fallback
that could not work for two thirds of its own stages.

**What to do.** Whatever provisions a shared resource must outlive every consumer
of it. The runner now starts and owns the container itself, before stage one, and
tears it down in `finally`. Where a resource is required by any stage, make it
required by the script — an optional parameter that half the stages depend on is
a trap with a friendly signature.

### Probe the dependency before running six stages against it

The first evidence package contained six copies of the same
`InvalidPasswordError`, spread across six logs, and the connection problem had to
be inferred from a stack trace forty frames deep.

**What to do.** One cheap probe up front — server version, extension version,
isolation level — turns "six stages failed" into "the database is unreachable,
and here is which user it tried." Where every later stage shares one dependency,
that probe is also the only place a hard stop is justified in an
otherwise-continue-on-failure runner.

### A manifest that counts lines cannot report an outcome

The evidence manifest listed each log and its line count. A run in which every
database stage failed produced a manifest indistinguishable from a clean one —
the failing logs were *longer*, because stack traces are verbose.

**What to do.** Record exit codes, print a verdict per stage and an overall
verdict, and put it at the top of the file. Volume of output correlates with
failure at least as often as with success.

### Do not send source code through a shell argument parser

The stage 03 probe was Python passed to `python -c` from a PowerShell
here-string. It arrived at the interpreter as `c.fetchval(SELECT extversion …)`
and died with `'(' was never closed`. The inner double quotes were stripped by
the **native-command argument parser**, which runs *after* PowerShell has
finished evaluating the string — so a here-string, single quotes, or any amount
of care with PowerShell's own quoting rules could not have prevented it. Nothing
about the string was wrong when PowerShell was done with it.

**What to do.** Write the program to a file and execute the file. There is no
argument-parsing boundary for the source to cross, the code is syntax-checkable
before it ships, and the file becomes part of the evidence — you can see exactly
what ran. The rule generalises: `-c`, `-e`, `bash -c`, `psql -c` and every
sibling are for one-liners with no quoting in them. Anything longer belongs in a
file.

### Do not create what the platform already guarantees

`CREATE EXTENSION IF NOT EXISTS timescaledb` failed with a duplicate key on
`pg_extension_name_index`. `IF NOT EXISTS` is not atomic against a concurrent
creator: it reads the catalogue, finds nothing, and collides when the image's own
init script commits first. The statement was redundant — the image installs the
extension into `template1`, so `POSTGRES_DB` already had it — and a redundant
statement can only ever fail.

**What to do.** Distinguish provisioning from verification. A precondition the
platform already guarantees should be *asserted*, not re-established. The check
is cheaper, cannot race, and gives a better error: "the extension is missing"
rather than "creating the extension failed", which are very different problems
wearing the same message.

### Byte-level things that cost real time

- `Tee-Object` on Windows PowerShell 5.1 writes **UTF-16LE**. The evidence was
  unreadable with ordinary text tooling until it was decoded. Write UTF-8
  explicitly.
- PowerShell 5.1 cannot parse a double-quoted string nested inside a `$()`
  subexpression of another double-quoted string. Compute the value first.
- `.pytest_cache` became unwritable mid-run on both Windows (`WinError 5`,
  `WinError 183`) and a Linux FUSE mount, aborting collection for reasons
  unrelated to the code. A full validation run has no use for `--lf` or `--ff`:
  pass `-p no:cacheprovider` and remove the failure mode.

---

## Estimation accuracy so far

Kept because estimates that are never checked never improve.

| Subsystem | Estimated | Revised to | Why the revision |
|---|---|---|---|
| S01 | 4 sessions | 4 | — |
| S02 | 4 sessions | 6 | ADR-035 moved observability from S41 into every component |
| S03 | 5 sessions | 7 | Sub-paise price precision, property testing, and rule R6 |

**Pattern: two consecutive upward revisions, both from scope discovered during
architecture rather than during implementation.** That is the architecture-first
process working — the discovery happened before code was written both times —
but it is also evidence that the roadmap's per-subsystem estimates are optimistic
by roughly 30%. Worth watching at S04 and S05 before adjusting the plan.

---

## S04 — Persistence

### Eager guard evaluation recurs wherever a path becomes hot

The defect fixed in S03's `Money` guards reappeared in `TradingDay.of` and
`SurrogateId.__init__` at S04 — not because they regressed, but because a path
that had been cold became hot. A `TradingDay` built once per request costs
nothing; one built per row read costs a third of the reconstruction budget.

```python
invariant(calendar.is_session(day), "...", day=day.isoformat())  # isoformat() every call
```

**The general rule:** `invariant(cond, msg, **ctx)` evaluates every argument
before the call. It is the right tool on cold paths and the wrong one on hot
ones, and *which paths are hot changes as the system grows*. Re-check the guards
in a module the first time it appears in a benchmark.

Measured: record→domain mapping 11.4 µs → 7.7 µs.

### Benchmark noise on a contended runner is indistinguishable from regression

A different sub-microsecond benchmark failed on each run of the same commit. That
signature — the failure *moving* — is measurement noise, not regression, and the
wrong response is to tune the threshold until it passes.

**What to do.** Gate sub-microsecond budgets behind an environment variable that
the runner sets only when it can resolve them (`DHRUVA_CANONICAL_BENCHMARKS`).
Millisecond-scale budgets are fine anywhere. Absolute thresholds remain correct;
the machine was the problem.

### Some costs belong to the framework, and the budget should say so

ORM object construction measured 13 µs against a 10 µs budget. That is
SQLAlchemy's declarative instrumentation for eight mapped columns, not anything
this codebase controls. The original figure was guessed without first measuring
the framework's baseline.

**What to do.** Measure the framework's floor *before* setting a budget that
includes it, then budget the delta you own. Revise with the reason recorded
(ADR-036) rather than chasing a number that was never achievable.

### `pytest-asyncio` event loops surface as failures in unrelated tests

A Hypothesis test failed intermittently, and a *different* one each run. The
cause was neither: pytest-asyncio creates an event loop per test, loops and their
sockets are collected later, CPython emits `ResourceWarning` from `__del__`, and
`filterwarnings = ["error"]` turned that into a failure attributed to whichever
test happened to trigger the collection.

**The tell.** When a failure moves between runs, suspect something *global* —
warnings, GC, module state — rather than the test it lands on.

**What to do.** Ignore the specific teardown warnings narrowly rather than
relaxing `error` generally, so a leak the codebase actually owns still fails.

### A `conftest.py` module-level `pytestmark` does not reach sibling modules

Integration tests errored during fixture setup instead of skipping cleanly,
because the `pytestmark` declaring them database-dependent lived in `conftest.py`
rather than in each test module.

**What to do.** Use `pytest_collection_modifyitems` to apply directory-wide
markers. It works, and a new module in that directory inherits them without
having to remember.

### A test that filters too broadly can pass vacuously

The migration-discipline check stripped every `ast.Expr` from a `downgrade()`
body to skip the docstring — which also discarded every `op.drop_table()` call,
since those are expression statements. It would have reported an empty downgrade
for any migration that actually did work.

Caught immediately because a real migration failed the check. **Worth
generalising:** when a test filters a collection before asserting on it, verify
the filter against a case that should *pass*, not only one that should fail.

---

## S03 — Mutation testing

### Property-test shrinking dominates mutation cost

**Symptom.** A mutation run that should have taken seconds per mutant was taking
tens of seconds, with no output for minutes at a time.

**Cause.** Mutation testing *kills* a mutant by making a test fail. When that
test is a Hypothesis property test, Hypothesis responds to the failure by
**shrinking** — searching for the smallest counterexample. Shrinking is expensive
and it is exactly what you want when a human is going to read the result.

A mutation harness never reads the result. It needs one bit: did the suite
notice? Every second spent minimising a counterexample is wasted, and it is spent
on the *successful* path, so the better the test suite the slower the run.

**What to do.** Register a Hypothesis profile that excludes the shrink phase, and
select it only for mutation runs:

```python
settings.register_profile(
    "fast",
    max_examples=15,
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target],
)
```

Measured effect: more than an order of magnitude on per-mutant cost.

### Never mutate in place

**Symptom.** After a mutation run was killed by a timeout, an unrelated test
started failing with `Money.of(0, 0)` raising "minor part is out of range". The
source file had also silently lost all of its comments.

**Cause.** The harness wrote each mutant over the original source and restored it
in a `try/finally`. `finally` does not run when the process is killed by a signal.
The tree was left holding a mutated file — and because the mutant was produced by
`ast.unparse`, the file had also lost every comment and every blank line, since
neither survives a round trip through the AST.

It was caught within minutes by a failing test, and recovered from git. Had it
been committed, the loss of comments would have been the more expensive half.

**What to do.** Mutate a **scratch copy of the tree**, never the working tree.
This makes the failure mode impossible rather than merely unlikely — no signal
handling, no cleanup path, nothing to get wrong:

```python
scratch = Path(tempfile.mkdtemp()) / repo.name
shutil.copytree(repo, scratch, ignore=shutil.ignore_patterns(".git", "__pycache__"))
```

The general principle: **a tool that modifies your source needs to be
crash-safe by construction, not by discipline.** Any design whose safety depends
on a cleanup step running is one `SIGKILL` from damaging the repository.

### `ast.unparse` is lossy in ways that matter

Comments and blank lines do not exist in the AST, so they do not survive
`ast.parse` → `ast.unparse`. Docstrings do survive, being string literals.

Relevant to any tool that rewrites source: codemods, formatters built on `ast`,
mutation harnesses. If comments matter — and in this codebase they carry the
*reasoning*, which is the part that cannot be reconstructed — use a
concrete-syntax-tree library such as LibCST, or do not rewrite source at all.

### Mutation tooling and Python version support

Two tools, two different blockers, both traceable to running on an interpreter
older than the target:

| Tool | Blocker |
|---|---|
| `mutmut` 2.x | Depends on `parso`, which cannot parse the `match` statement. 15 parse errors on a single file. |
| `mutmut` 3.x | Parses correctly, but runs tests from a copied `mutants/` directory in a way that conflicts with a `sitecustomize` compatibility shim. |

**What to do.** Check that a mutation tool parses your *actual* source before
adopting it — `parso.load_grammar().iter_errors(parso.parse(src))` answers this in
one line. And prefer running mutation analysis on the canonical interpreter
rather than a compatibility-shimmed one, because the shim is itself a source of
tool incompatibility.

---

## S03 — Performance

### Guard helpers that evaluate arguments eagerly

**Symptom.** `Money + Money` measured 1.856 µs against a 0.50 µs budget — 3.7×
over — with no obvious hot spot in the arithmetic.

**Cause.** The guard was written as:

```python
invariant(
    self._currency is other._currency,
    f"cannot {operation} amounts in different currencies",   # built every call
    left=str(self._currency),                                 # called every call
    right=str(other._currency),                               # called every call
)
```

Python evaluates arguments before the call. So every addition — on the success
path, which is all of them — built an f-string, made two `str()` calls, and
constructed a keyword dictionary, before `invariant` had the chance to check
anything.

**What to do.** On hot paths, check first and construct the message only on
failure. Keep the failure branch in a separate cold method so it does not inflate
the hot one:

```python
if self._currency is not other._currency:
    self._reject_currency_mismatch(other, "add")
```

Measured effect: 1.856 → 0.456 µs; one million additions from 2.007 s to 0.545 s.

`invariant(...)` remains the right tool everywhere else. The lesson is not that
the helper is bad — it is that **an ergonomic helper has a cost, and the cost is
only visible when someone measures.**

### Coverage, types and architecture cannot see performance

This defect passed every other gate: 99% coverage, `mypy --strict` clean,
architecture conforming, all tests green. It was found only because ADR-036
required a measured budget declared before implementation.

That is the argument for quantitative budgets, in one example.

### Measuring microsecond operations

A per-iteration `time.perf_counter()` costs a meaningful fraction of what it
measures, and a per-iteration p99 mostly reports garbage collection.

Use **best-of-batched-means** below roughly 10 µs: time an inner loop of N calls
once, repeat R times, take the minimum. Use tail measures (p95/p99) above that,
where the timer is negligible and the tail is what an operator experiences.

Discovering this mid-implementation produced 5× run-to-run variance before the
change. Record the methodology next to the numbers; a benchmark without its
method is a number without meaning.

---

## S02 — Logging and configuration

### A logger that binds its stream cannot be tested

structlog's `PrintLogger` captures `sys.stderr` at construction. Anything that
replaces `sys.stderr` afterwards — pytest's capture, a supervisor, a
`redirect_stderr` block — silently stops seeing output.

For an ordinary logger that is a nuisance. For a **security control** such as
redaction it is disqualifying: a control whose output cannot be captured is a
control nobody can verify. Resolve the stream per call instead.

### Eager redaction snapshots

The redaction processor rebuilt its set of known secret values on **every log
record** — two set constructions per line. Cached against a version counter:
71.9 → 36.7 µs per emission.

Same family of mistake as the `invariant` case above: work done on the common
path that only matters on the rare one.

---

## S01 — Architectural enforcement

### import-linter cannot express "only via the public API"

Layer ordering inside a package it expresses natively and well. "Context A may
reach context B, but only through `B.api`" requires enumerating every source and
target pair — 72 hand-written contracts for nine contexts, which would rot on the
first rename.

Purpose-built AST checks are the right tool for reachability rules. Keep the
policy as a single readable dictionary and let the checker walk the real import
graph.

### Test every control against a deliberate breach

Each architectural control here is tested twice: once against the real codebase,
and once against a synthetic tree that deliberately violates it.

A green control that *cannot* fail is worse than no control, because it is
trusted. The ADR guard demonstrated this by catching its own author — seven
approved decisions had been discussed and never written as records, and the
numbering-gap check found it.
