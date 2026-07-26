# S03 — Approval Package

**Domain Primitives & Shared Kernel.** Tag `v0.3.0` created locally, working tree
clean, awaiting your final review.

---

# 1. Executive Summary

## What was built

The vocabulary every future part of D.H.R.U.V.A speaks: **money, prices,
quantities, rates, trading days, time, identity and domain events.**

Roughly forty-three subsystems remain, and every one of them will import from
here. A backtest will construct millions of these objects. Every rupee the
platform ever reports passes through `Money`.

## Why it matters

**These are the types where a bug is silent and expensive.** A wrong boundary
rule fails the build. A wrong rounding policy produces plausible numbers that are
quietly wrong — and by S12 the cost engine, by S24 the P&L, and by S30 every
backtest inherit the error. Nothing crashes. The reports simply do not match a
contract note, and the first person to notice may be you, reconciling a real
trade months later.

So the design makes whole categories of financial bug **unexpressible** rather
than merely discouraged. `Money + Price` does not compile. A float cannot enter
the monetary layer. A quantity cannot be negative. A trading day cannot exist
without a calendar that verified it. Splitting ₹100 three ways cannot lose a
paisa.

## What changed

**The headline finding: paise are not enough precision.** NSE currency
derivatives quote to four decimal places and tick at ₹0.0025. A price fixed at
paise scale would have silently rounded every one of them. This forced ADR-042,
which supersedes ADR-005 — the first supersession in the project, handled through
the process rather than around it.

**Benchmarking found a defect no other gate could see.** The first run came in
3.7× over budget. `invariant()` evaluates its arguments eagerly, so every single
addition was building an f-string and calling `str()` twice before checking
anything. Coverage was 99%, types were clean, architecture conformed, all tests
passed. Only a measured budget surfaced it. Fixed: **1.856 µs → 0.456 µs**.

**The shared kernel is now API-stable** (ADR-050). Any change to these types
requires a dedicated ADR and your approval.

**865 tests. 98.6% coverage. Six of seven gates green.** The seventh — mutation
testing — could not run in the build environment, and that is declared as an
exception below rather than omitted.

---

# 2. Engineering Summary

## Architecture decisions

| ADR | Decision | The reasoning that mattered |
|---|---|---|
| **042** | Money as integer minor units; Price at fixed 8 dp *(supersedes ADR-005)* | Paise cannot represent a ₹0.0025 currency-derivative tick. Separately: `Decimal` reads a thread-local context, so two processes configured differently produce different answers — a determinism defect one layer below ADR-011. |
| **043** | Dimensional typing | `Money × Money` is rupees-squared. Enforced twice: statically so it fails at check time, at runtime so untyped callers get `TypeError` not `AttributeError`. |
| **044** | Explicit named rounding | A reviewer holds a SEBI circular, not a numerical-analysis textbook. `STT_NEAREST_RUPEE` is checkable against the former. |
| **045** | Unsigned `Quantity`, explicit `Side` | A wrongly-signed position reconciles against nothing and produces a plausible P&L. |
| **046** | Calendar-verified `TradingDay` | NSE moved expiry Thursday→Tuesday on 2025-09-01. Date arithmetic is wrong across it and nothing flags it. |
| **047** | Exceptions only, no `Result` | Two idioms means the boundary is relitigated in every review for a decade. |
| **048** | Rule R6, no float under `shared/money` | Bans literals, conversions and annotations; permits `isinstance` guards, which *reject* floats. |
| **049** | Mutation testing | Catches what coverage structurally cannot: a test that executes a line without asserting anything. |
| **050** | Shared kernel API-stable | Churn here is not a local cost. Carve-outs keep new policies, currencies and event subclasses open. |

## Risks

| Risk | Status |
|---|---|
| **R02 — silent research invalidity** | **Substantially reduced.** Exact arithmetic, lossless allocation, explicit rounding, no float, conservative estimates. The main remaining vector is adoption, not the primitives. |
| **R13 — architectural decay** | **Reduced.** Six build-enforced rules now (R1–R6). |
| **New — test quality unverified** | **Open, HIGH (TD-12).** Coverage is 98.6% but mutation testing has not run. Every claim about test *quality* rests on coverage and on reading. |
| **R01 — scope collapse** | **Watch.** S03 grew 5→7 sessions on an approved basis. Third consecutive subsystem to expand. |
| **R15 — bus factor** | **Reduced.** `DOMAIN.md` is 719 lines of *reasoning*, which is what survives a six-month gap. |

## Benchmarks

Environment: Intel Core i5-10300H @ 2.50 GHz, 2 vCPU, 3 GB, Linux 6.8.0,
**CPython 3.10.12** (target is 3.12, so figures are conservative).
Method: best-of-batched-means for µs-scale, wall clock for the aggregate.

**11 of 13 pass.** Two marginal misses — `Money` construction 0.374 µs vs 0.30,
and the million-addition aggregate 0.545 s vs 0.50 — same cause seen twice,
recorded as TD-13/TD-14 rather than dropped.

## Remaining work

Mutation testing (TD-12). Then S04 — Persistence Foundation.

---

# 3. Portfolio Summary

> **D.H.R.U.V.A** — Institutional-grade quantitative analytics platform for
> Indian equity and derivatives markets (NSE/BSE, Zerodha Kite).
>
> Designed a financial domain kernel that makes whole categories of monetary bug
> **unrepresentable**: exact integer money with no floating point anywhere in the
> monetary path, dimensional typing so `Money + Price` fails to compile,
> rounding that must be explicitly chosen from policies named after SEBI and
> exchange rules, lossless value allocation proven by property test, and trading
> days that cannot be constructed without a verifying exchange calendar.
>
> Enforced by six custom build-time architectural rules — bounded-context
> boundaries, configuration isolation, and a float ban in the monetary layer —
> plus a checksum-based guard making the 50-record architecture decision log
> immutable once accepted. Two production defects were caught by tooling rather
> than review: an eager-evaluation performance bug (3.7× over budget, invisible
> to 99% coverage) and a serialisation failure that would have surfaced only
> after distributed workers were introduced.
>
> **Stack:** Python 3.12 · FastAPI · Pydantic · structlog · PostgreSQL/TimescaleDB ·
> Redis · Prometheus · OpenTelemetry · Docker · GitHub Actions
> **Practices:** Clean Architecture · DDD · ADR-driven design · property-based
> testing (Hypothesis) · `mypy --strict` · measured performance budgets · 98.6%
> coverage across seven automated quality gates

**Resume line:**

> Built the financial domain kernel for a quantitative trading platform where
> monetary bug classes are unrepresentable by construction — exact integer money,
> dimensional typing, mandatory explicit rounding — enforced by six custom
> build-time architectural rules and 865 property-based tests.

---

# 4. Git history and commit plan

Branch `s03-domain-primitives`, **6 commits**, merged `--no-ff` into `main`,
tagged `v0.3.0`.

```
*   59dda50  Merge pull request #3 from drew/s03-domain-primitives
| * 878c155  docs(s03): add DOMAIN.md, ADR-042 through ADR-050 and release artefacts
| * 190a249  test(shared): add the S03 budget suite and a stopgap mutation harness
| * 3299360  perf(shared): rewrite hot-path guards after benchmarking
| * b26b7d9  feat(shared): add time, identity and domain-event primitives, plus rule R6
| * 0713f54  feat(shared): add the monetary primitives -- Money, Price, Quantity, Ratio
| * 647a6d7  docs(s03): add Domain Primitives architecture and design document
|/
*   266a132  docs: correct changelog compare links to the real repository
```

**PR title:** `S03 — Domain Primitives & Shared Kernel`
**PR summary:** the merge commit body.
**Tag:** `v0.3.0`, annotated, created.

## Push commands

```bash
cd C:\Users\dhruv\Projects\DHRUVA

git log --oneline --graph --all
git status

git push origin main
git push origin v0.3.0

# Optional, so the PR is visible in GitHub's UI:
git push origin s03-domain-primitives
```

Verify:

```bash
git ls-remote --tags origin        # expect v0.1.0, v0.2.0, v0.3.0
git log origin/main --oneline | head -3
```

---

# 5. Approval request

S03 is complete against its Definition of Done **with one stated exception**:

> **Mutation testing (ADR-049) did not execute.** Three tooling routes were
> blocked by the Python 3.10 sandbox. No score is reported because no honest
> score is available. Commands are supplied for a machine without a per-command
> time ceiling; recorded as **TD-12, HIGH**.

I am requesting approval **with that exception explicit**, rather than claiming a
clean Definition of Done. If you would prefer S03 stay open until a mutation
score exists, that is a reasonable call and I will hold it open.
