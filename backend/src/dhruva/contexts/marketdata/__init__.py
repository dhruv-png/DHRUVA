"""C2 -- marketdata bounded context.

Ticks, bars, quotes, open interest, subscriptions, historical backfill.

Publishes
---------
TickBatchIngested, BarClosed, BackfillCompleted

Depends on
----------
May import ``reference``, and only through each one's public ``api`` module.
Enforced by ``tests/unit/test_context_boundaries.py``.

Stage
-----
Active in Stage 1 (MCP).

See Master Project Plan section 5.
"""
