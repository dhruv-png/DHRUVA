"""Market data ingestion composition root (``dhruva-ingest`` process).

Long-lived process owning the broker WebSocket lifecycle and the
subscription budget. Composition root: may import any context's
``infrastructure``.
"""
