"""``dhruva.workers.entrypoint`` -- the composition root Celery is pointed at.

This is the ``--app`` target, and the only place in the worker process permitted
to read configuration and wire adapters::

    celery --app 'dhruva.workers.entrypoint:create_app()' worker --queues dhruva
    celery --app 'dhruva.workers.entrypoint:create_app()' beat

The parentheses matter. Celery's ``--app`` accepts an instantiation expression,
which is what lets :func:`create_app` be a function rather than the module-level
``app`` object the conventional layout uses. That object would be an ambient
global holding validated configuration, built as a side effect of an import --
the thing ADR-031 exists to remove.

One startup sequence, not two
-----------------------------
:func:`~dhruva.shared.runtime.bootstrap` is what the API composition root
already calls, and calling it here means a worker's log records carry the same
service identity, the same UTC timestamps and the same redaction processor as
the API's. A second startup path written for workers would drift from the first,
and the drift would be discovered the day a credential appeared in a worker log
and not in an API one.

The health and metrics registries it returns are not used yet. A worker serves
no HTTP, so nothing scrapes them; they are left unused rather than not built,
because building them is what makes the sequence identical.

Why no beat entries are registered here
---------------------------------------
A ``TradingDayBeatSchedule`` resolves through a :class:`TradingCalendar`, and no
implementation of that port exists yet -- only the port itself. Registering
entries now would mean inventing a calendar to satisfy them, and a schedule
resolved against an invented calendar is a schedule that runs on the wrong days
while looking configured. The entries arrive with the first real calendar
adapter; the beat schedule they will use is written and tested (ADR-066).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.shared.runtime import bootstrap
from dhruva.workers.celery_app import build_celery_app
from dhruva.workers.registry import install

if TYPE_CHECKING:
    from pathlib import Path

    from celery import Celery

__all__ = ["SERVICE_NAME", "create_app"]

#: The name stamped on every log record and span this process emits.
#:
#: Distinct from the API's ``dhruva-api``, because "which process was this?" is
#: the first question asked of a log line and the last one a shared name can
#: answer.
SERVICE_NAME = "dhruva-worker"


def create_app(env_file: Path | None = None) -> Celery:
    """Bring the worker runtime up and return its Celery application.

    Parameters
    ----------
    env_file
        Optional ``.env`` read before the process environment, passed through to
        :func:`~dhruva.shared.runtime.bootstrap`. Environment variables always
        win, so a deployment cannot be overridden by a file left in an image.

    Returns
    -------
    Celery
        A configured application. Building one opens no connection, so a worker
        that cannot reach Redis fails when it first uses the broker -- with the
        broker's own error -- rather than during argument parsing.

    Raises
    ------
    ConfigurationError
        If configuration is missing or malformed. Raised here, before the worker
        starts consuming, which is the point of fail-fast: a worker that started
        successfully and failed on its first job at 09:16 would have taken that
        job off the queue to do it.
    UnsafeConfigurationError
        If configuration is valid but unsafe for its environment.

    Notes
    -----
    Every call returns a new application, and none becomes Celery's current one.
    A task must therefore be registered with ``@app.task`` on the application
    this returns -- never with ``@celery.shared_task``, which resolves through
    whichever application is current and would find one nobody configured.
    """
    context = bootstrap(service=SERVICE_NAME, env_file=env_file)
    app = build_celery_app(context.settings.redis)
    # No calendar is supplied, so a scheduled job would refuse to install here --
    # deliberately, because no `TradingCalendar` implementation exists yet and a
    # schedule resolved against an invented one runs on the wrong days while
    # looking configured. The registry is empty, so nothing refuses today.
    install(app)
    return app
