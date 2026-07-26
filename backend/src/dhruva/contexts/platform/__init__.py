"""C9 -- platform bounded context.

Authentication, accounts, secrets, configuration, audit, jobs, feature flags.

Publishes
---------
AuditRecorded

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
