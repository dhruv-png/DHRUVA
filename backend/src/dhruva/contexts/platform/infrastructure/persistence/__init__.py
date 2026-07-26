"""The four-layer persistence flow, demonstrated end to end.

::

    Database row
        -> SQLAlchemy model      models.py      persistence shape only
        -> Persistence record    records.py     primitives, no domain types
        -> Mapper                mappers.py     pure, benchmarkable, no services
        -> Reconstruction factory factories.py  holds domain services
        -> Domain object

Amendment 2 to the S04 design added the record and factory steps. The reason is
``TradingDay``: it cannot exist without a calendar (ADR-046), a mapper has no
business holding one, and a repository quietly constructing infrastructure
dependencies would be its own problem. Separating them keeps the mapper pure --
which is what makes it independently benchmarkable -- and keeps reconstruction
in one place that legitimately owns domain services.
"""
