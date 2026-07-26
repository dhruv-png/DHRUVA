# Research

Notebooks and exploratory analysis.

**This directory is not production code, and production code may not import from
it** (ADR-025). It is excluded from the type and lint gates, which is exactly why
it must stay quarantined: notebook code reads global state, is rarely tested, and
is written fast.

Promoting a result into the platform means rewriting it against the domain model
with tests. That translation step is deliberate and is budgeted in every subsystem
estimate. A test enforces the import ban.

## Layout

```
research/
├── notebooks/    # exploratory work, committed
├── data/         # local scratch data - gitignored
└── output/       # generated figures and tables - gitignored
```
