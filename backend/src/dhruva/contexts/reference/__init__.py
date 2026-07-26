"""C1 -- reference bounded context.

Instruments, trading calendar, sessions, corporate actions, F&O ban list.

Publishes
---------
InstrumentUpdated, CorporateActionAnnounced, SessionOpened, SessionClosed

Depends on
----------
May import nothing (it is a root of the dependency graph), and only through each one's public
``api`` module.
Enforced by ``tests/unit/test_context_boundaries.py``.

Stage
-----
Active in Stage 1 (MCP).

See Master Project Plan section 5.
"""
