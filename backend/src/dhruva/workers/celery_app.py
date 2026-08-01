"""The Celery application (ADR-062, ADR-066).

Configuration is the whole of this module. Celery runs *scheduled and deferred
work* -- nightly reconciliation, ledger pruning, corporate-action ingestion. It
is not the event bus, and it is not the outbox relay, which is a dedicated
process for the three reasons ADR-063 gives.

Every setting below is stated explicitly, including the several whose value
matches Celery's own default. A default is a decision someone else made and may
revise in a minor release; a default that is right today and silently different
after an upgrade changes this system's behaviour with no diff to review. Written
down, the same change becomes a conflict in a file whose every line has a reason
beside it.

Why there is no module-level ``app``
------------------------------------
The conventional Celery layout builds an application at import time and lets
every task module reach for it. That is an ambient global holding validated
configuration -- unable to be isolated in a test, and reading the environment as
a side effect of an import. ADR-031 exists to remove exactly that.

So the application is built by a factory from a settings slice the composition
root passes in. Celery's ``--app`` accepts an instantiation expression, so
nothing is lost operationally::

    celery --app 'dhruva.workers.entrypoint:create_app()' worker

``set_as_current=False`` follows from the same decision and has one consequence
worth stating plainly: a task must be registered on the injected application with
``@app.task``, never with ``@celery.shared_task``. A shared task resolves through
whichever application happens to be current, which is the global this design does
not have.

At-least-once, restated for jobs
--------------------------------
ADR-062 fixes delivery at at-least-once and makes every consumer's idempotency an
obligation rather than a nicety. Late acknowledgement extends the same promise,
and the same obligation, to jobs. A worker killed mid-job has not acknowledged
it, so the job runs again; a job still running at the end of
:data:`VISIBILITY_TIMEOUT` is redelivered *while the first copy is still
working*. Neither is a defect to be configured away -- the alternative is
acknowledging on receipt, which converts a redelivery into a job that silently
never ran. Both are why a job that is not idempotent (ADR-065) is a defect.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from celery import Celery

if TYPE_CHECKING:
    from dhruva.shared.config.settings import RedisSettings

__all__ = [
    "APP_NAME",
    "DEFAULT_QUEUE",
    "TASK_MODULES",
    "VISIBILITY_TIMEOUT",
    "build_celery_app",
]

#: The application's name. Celery prefixes the task names it generates with it
#: and reports it to ``celery inspect``, so it is what an operator sees.
APP_NAME: Final = "dhruva"

#: The queue jobs are routed to unless a task declares otherwise.
#:
#: Named rather than left as Celery's ``celery``. The broker is a shared Redis,
#: and two applications defaulting to the same queue name on the same database
#: consume each other's work -- a symptom ("a job vanished") that points nowhere
#: near its cause.
DEFAULT_QUEUE: Final = "dhruva"

#: Modules the worker imports so their tasks register. Empty until a job exists.
#:
#: Declared rather than discovered. ``autodiscover_tasks`` finds task modules by
#: naming convention, so a module in the wrong place registers nothing at all and
#: the worker still starts cleanly, reports no error, and never runs the job. A
#: declared tuple fails at startup with the import error that says which module,
#: and it can be grepped for.
TASK_MODULES: Final[tuple[str, ...]] = ()

#: How long the broker waits for an acknowledgement before redelivering a job.
#:
#: Must outlast the longest job this application runs. Redis has no server-side
#: notion of work in progress: with late acknowledgement, kombu re-queues
#: anything unacknowledged after this window, so a backfill still working at the
#: end of it is started a second time alongside the first. Six hours clears an
#: overnight backfill comfortably; kombu's own default of one hour does not.
VISIBILITY_TIMEOUT: Final = timedelta(hours=6)


def build_celery_app(redis: RedisSettings) -> Celery:
    """Build the Celery application from validated configuration.

    Parameters
    ----------
    redis
        Broker connection settings. A typed slice rather than the whole
        :class:`~dhruva.shared.config.settings.Settings`: this needs a host, a
        port, a database and a credential, and a component handed everything is a
        component that knows too much (ADR-031).

    Returns
    -------
    Celery
        A configured application. Building one opens no connection, so a process
        that cannot reach Redis fails where it first uses the broker rather than
        at import, and a test can build one with no broker running at all.

    Notes
    -----
    Every call returns a new application and none of them becomes the current
    one, so two callers cannot configure each other's.
    """
    app = Celery(main=APP_NAME, set_as_current=False)
    app.conf.update(_configuration(redis))
    return app


def _configuration(redis: RedisSettings) -> dict[str, Any]:
    """Return the complete Celery configuration, one decision per key.

    Parameters
    ----------
    redis
        Broker connection settings.

    Returns
    -------
    dict[str, Any]
        Settings in Celery's modern lowercase namespacing, ready for
        ``app.conf.update``. A fresh dictionary each call: a module-level
        constant would be shared mutable state one application could edit out
        from under another.
    """
    return {
        # -- broker ------------------------------------------------------------
        # The event streams share this Redis database. Celery's keys are its
        # queue names and the ``_kombu.*`` namespace; the streams live under
        # their own prefixes, so the two cannot collide.
        "broker_url": _broker_url(redis),
        # A worker started before Redis is up should wait for it rather than
        # exit. Deployments start containers in whatever order the scheduler
        # chooses, and a worker that dies because it won the race is an alert
        # about nothing.
        "broker_connection_retry_on_startup": True,
        "broker_transport_options": {"visibility_timeout": int(VISIBILITY_TIMEOUT.total_seconds())},
        # -- results -----------------------------------------------------------
        # There is no result backend. Nothing asks Celery what a job returned:
        # outcomes are observed as events (ADR-062) and log lines. A backend
        # would be a second store of truth, with its own expiry, that no reader
        # would notice going stale.
        "result_backend": None,
        "task_ignore_result": True,
        # -- serialisation -----------------------------------------------------
        # JSON in both directions, and nothing else accepted. Pickle over a
        # broker is remote code execution for anyone who can write to the queue,
        # and Redis is not a trust boundary (S05 section 16). Celery already
        # defaults to JSON; saying so makes any future widening a visible diff.
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        # -- time --------------------------------------------------------------
        # Every instant crossing this boundary is timezone-aware UTC (ADR-006).
        # With ``enable_utc`` off, Celery hands beat a naive ``now``, and a naive
        # instant compared against a session boundary is a number compared
        # against a time.
        "enable_utc": True,
        "timezone": "UTC",
        # -- delivery ----------------------------------------------------------
        # Acknowledge after the job, not on receipt: losing a job is worse than
        # running one twice, which is ADR-062's reasoning applied to work rather
        # than to events. ``task_reject_on_worker_lost`` completes it -- without
        # it, a late-acknowledging job whose worker is killed outright is dropped
        # silently, which is the failure late acknowledgement exists to prevent.
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        # One job in hand at a time. Celery's default of four is tuned for short
        # tasks; the work here is backfills, backtests and model training, and a
        # worker that reserved four of them would leave three queued behind an
        # hour of computation while other workers sat idle.
        "worker_prefetch_multiplier": 1,
        # -- logging -----------------------------------------------------------
        # structlog owns logging (ADR-033), as it does under uvicorn. Celery
        # would otherwise replace the root logger's handlers at startup and every
        # record after that would bypass the redaction processor -- which is the
        # control that keeps credentials out of the log (ADR-037).
        "worker_hijack_root_logger": False,
        "worker_redirect_stdouts": False,
        # -- routing -----------------------------------------------------------
        "task_default_queue": DEFAULT_QUEUE,
        "imports": TASK_MODULES,
    }


def _broker_url(redis: RedisSettings) -> str:
    """Return the broker URL Celery connects with.

    Parameters
    ----------
    redis
        Broker connection settings.

    Returns
    -------
    str
        A ``redis://`` URL, carrying the password when one is configured.

    Notes
    -----
    The returned string is a credential. It is handed straight to Celery and must
    never be logged, formatted into an error, or used as a metric label. Private
    for that reason: the fewer places that can produce one, the fewer that can
    leak one.

    The password is percent-encoded with nothing left safe, because a password
    containing ``@``, ``/`` or ``:`` would otherwise re-parse as a host, a
    database number or a port. That failure is not an error -- it is a successful
    connection to somewhere nobody intended.

    TLS is not expressible yet: :class:`RedisSettings` carries no certificate
    configuration. When it does, the scheme here becomes ``rediss://`` and
    nothing else in this module changes.
    """
    credential = f":{quote(redis.password.reveal(), safe='')}@" if redis.password else ""
    return f"redis://{credential}{redis.host}:{redis.port}/{redis.db}"
