"""FastAPI composition root (``dhruva-api`` process).

Wires adapters to ports and mounts each context's ``interfaces`` routers.
As a composition root this package is permitted to import any context's
``infrastructure`` -- it is the only place in the codebase that is.
"""
