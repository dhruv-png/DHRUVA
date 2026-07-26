"""C5 -- strategy bounded context.

Strategy kernel, signal generator, backtesting, walk-forward optimisation.

Publishes
---------
SignalGenerated, BacktestCompleted

Depends on
----------
May import ``reference``, ``marketdata``, ``analytics``, ``intelligence``, ``risk``, and only
through each one's public ``api`` module.
Enforced by ``tests/unit/test_context_boundaries.py``.

Stage
-----
**Stage 2 only.** Per ADR-028 this context exists in Stage 1 as an empty,
contract-enforced boundary so that the layout is settled and cannot drift.
It contains no logic and must not until Gate G-MCP passes.

See Master Project Plan section 5.
"""
