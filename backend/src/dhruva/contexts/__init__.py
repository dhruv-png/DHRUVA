"""Bounded contexts.

Each subpackage is one bounded context from Master Project Plan section 5.
Contexts communicate only through each other's public ``api`` module or by
publishing domain events. No context reads another context's tables.

The dependency matrix is declared once, as data, in
``tests/unit/test_context_boundaries.py`` and enforced against the real import
graph on every commit.
"""
