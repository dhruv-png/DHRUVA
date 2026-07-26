"""Shared kernel.

Types that genuinely belong to every context: ``Money``, ``Quantity``,
``Price``, ``TradingDay``, ``InstrumentId``, ``Result``, the ``Clock`` port and
the domain-event envelope.

Rules
-----
* ``shared`` imports nothing from ``dhruva.contexts``. Ever.
* A type belongs here only if at least three contexts need it. Anything less is
  premature generalisation and belongs in the context that owns it.

Populated by S03 (Domain Primitives & Shared Kernel).
"""
