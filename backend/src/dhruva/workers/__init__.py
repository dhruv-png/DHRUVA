"""Celery composition root (``dhruva-worker`` and ``dhruva-scheduler``).

Hosts backfills, backtests, model training and calendar-aware scheduled
jobs. Composition root: may import any context's ``infrastructure``.
"""
