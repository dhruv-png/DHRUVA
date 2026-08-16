# Technical research candidates

The candidate command reads only the local PostgreSQL reference, daily-bar, and
research archives. It never contacts Zerodha, GDELT, NSE, or an order endpoint.

Inspect the current state without recording an official observation:

```powershell
Set-Location C:\Users\dhruv\Projects\DHRUVA\backend
& .venv\Scripts\dhruva-research.exe candidates --account owner-family
```

Show deterministic supporting, counter, and missing evidence, or reproduce a
historical knowledge cutoff:

```powershell
& .venv\Scripts\dhruva-research.exe candidates --account owner-family --detail
& .venv\Scripts\dhruva-research.exe candidates --account owner-family --as-of 2026-08-14T10:00:00+00:00 --detail
```

After reviewing the result, explicitly append the official weekly freeze:

```powershell
& .venv\Scripts\dhruva-research.exe candidates --account owner-family --freeze
```

Repeating the same account, cutoff, revisions, and state reports the same
fingerprint and does not create another row. The command ranks nothing when the
local database lacks essential history; it reports each instrument as
`DATA_UNAVAILABLE`, `INSUFFICIENT_HISTORY`, `BENCHMARK_UNAVAILABLE`, `STALE`, or
`UNSUPPORTED_ADJUSTMENT` as appropriate. Load historical data separately after
the credential-vault key is available. Do not interpret the output as advice:
the baseline remains experimental until walk-forward evaluation passes later
readiness gates.

Prospective freezes can now mature into separate append-only 20/60-session
outcomes, and the frozen baseline can be replayed as a clearly biased local
diagnostic. See `docs/runbooks/model-evidence.md` and ADR-081. Evaluation never
tunes the v0 weights or turns the result into an instruction.
