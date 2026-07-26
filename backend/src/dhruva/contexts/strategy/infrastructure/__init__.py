"""Infrastructure layer.

ADAPTERS: SQLAlchemy repositories, broker clients, message-bus adapters,
HTTP clients. Implements the ports declared in ``domain``.

May import this context's ``domain`` and ``application``. Nothing outside a
composition root may import this package (ADR-003).
"""
