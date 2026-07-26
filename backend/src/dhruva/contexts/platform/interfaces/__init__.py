"""Interfaces layer.

Delivery mechanisms: FastAPI routers, WebSocket handlers, CLI commands and
Celery task entrypoints.

May import this context's ``application`` and ``domain``. May not import
``infrastructure`` -- wiring happens at the composition root.
"""
