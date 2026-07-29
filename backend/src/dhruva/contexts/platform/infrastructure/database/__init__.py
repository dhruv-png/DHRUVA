"""Persistence machinery.

SQLAlchemy lives here and nowhere above. Boundary rule R7 fails the build if any
domain layer imports a persistence framework (ADR-052, ADR-059).
"""
