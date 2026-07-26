"""C3 -- analytics bounded context.

Indicators, breadth, futures analytics, options analytics, regime, institutional flow.

Publishes
---------
RegimeChanged, AnalyticsComputed

Depends on
----------
May import ``reference``, ``marketdata``, and only through each one's public ``api`` module.
Enforced by ``tests/unit/test_context_boundaries.py``.

Stage
-----
Active in Stage 1 (MCP).

See Master Project Plan section 5.
"""
