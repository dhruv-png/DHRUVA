"""Registering a job on the worker application (ADR-031, ADR-033, ADR-038).

A job is a plain function. It takes no Celery types, imports no Celery, and is
tested by calling it. This module is what turns one into a task, and it is the
only place in the worker that knows Celery has tasks at all.

Why registration is a call, not a decorator on the function
------------------------------------------------------------
The conventional layout decorates at import time -- ``@app.task`` on the
function, or ``@shared_task`` when the app is not reachable from there. Neither
is available here. There is no module-level application to decorate with
(ADR-031), and ``shared_task`` resolves through whichever application happens to
be *current*, which is precisely the ambient global this design removes: with
``set_as_current=False`` it would find one nobody configured and queue work onto
a broker nobody chose.

So the composition root registers. The consequence is worth the trade: a job
module contains functions and nothing else, so a job can be unit-tested without
a broker, without an application, and without Celery installed at all.

Diagnosis is written down here, where it still exists
------------------------------------------------------
An exception raised in a worker is pickled to reach whoever is watching, and
``BaseException.__reduce__`` carries no ``__cause__`` and no ``__traceback__``
(there is a test that pins this). A failure logged only by the observer is
therefore a failure stripped of the chain that explains it.

So the failure is logged *in the worker*, before it crosses, where the cause and
the traceback are still attached -- and then re-raised unchanged. Unchanged
matters: wrapping it would give Celery's own retry and failure handling a type
the job did not raise, and would discard the original for a summary of it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from dhruva.shared.context import bind_correlation, current_correlation_id
from dhruva.shared.errors import ValidationError
from dhruva.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from celery import Celery

__all__ = ["CORRELATION_HEADER", "JOB_NAME_PREFIX", "register_job"]

_log = get_logger(__name__)

#: The message header a caller puts a correlation id in.
#:
#: A header rather than a task argument. An argument would appear in every job's
#: signature, so every job would have to accept and ignore it, and one that
#: forgot would fail at call time on an argument that has nothing to do with what
#: it does. A header travels beside the payload and no job ever sees it.
CORRELATION_HEADER: Final = "dhruva-correlation-id"

#: Every job name begins with this.
#:
#: The broker is a shared Redis. A bare name like ``reconcile`` is one another
#: application can also register, and the failure -- two systems consuming each
#: other's work -- presents as jobs vanishing, which points nowhere near its
#: cause. The prefix also makes ``celery inspect registered`` greppable.
JOB_NAME_PREFIX: Final = "dhruva."

#: Names that survive being a Redis key, a log field and a metric label.
_SAFE_JOB_NAME: Final = re.compile(r"\Adhruva\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\Z")


def register_job(app: Celery, func: Callable[..., Any], *, name: str) -> Any:
    """Register ``func`` as a task on ``app``, and return the task.

    Parameters
    ----------
    app
        The application from the composition root. Explicit, because
        ``shared_task`` would resolve through a current application that this
        design deliberately does not have.
    func
        The job. An ordinary function: it receives no task instance, and nothing
        about it changes by being registered.
    name
        The task's name on the wire, which must begin with ``dhruva.``. Stated
        rather than derived from ``func.__qualname__``: a derived name changes
        when a function is renamed or moved, and messages already queued under
        the old one are then unroutable -- work that vanishes on a deployment.

    Returns
    -------
    Any
        Celery's task object. Typed loosely because Celery ships no stubs; the
        caller needs it only to enqueue work.

    Raises
    ------
    ValidationError
        If the name is not one that survives being a Redis key, a log field and
        a metric label.

    Notes
    -----
    The registered task binds the inbound correlation id for the duration of the
    call, so every record the job emits carries it, and logs the failure before
    re-raising so the cause survives a boundary that would otherwise drop it.
    """
    if not _SAFE_JOB_NAME.match(name):
        raise ValidationError(
            f"a job name must be lowercase, dot-separated and begin with {JOB_NAME_PREFIX!r}",
            name=name,
            expected=_SAFE_JOB_NAME.pattern,
        )

    def run(task: Any, *args: Any, **kwargs: Any) -> Any:
        """Bind correlation, run the job, and record a failure before it travels.

        Takes the task instance because ``bind=True`` below supplies it, which is
        how the inbound message's headers are reachable. The job itself never
        sees it: it is consumed here and the remaining arguments are passed on
        untouched.
        """
        with bind_correlation(correlation_id=_inbound_correlation_id(task)):
            try:
                return func(*args, **kwargs)
            except Exception:
                # Logged here rather than left to the observer: the cause and the
                # traceback are attached in this process and in no other.
                _log.exception("job failed", job=name)
                raise

    run.__name__ = func.__name__
    run.__doc__ = func.__doc__

    # `shared=False` is load-bearing, and its default is not what it sounds like.
    # `Celery.task()` defaults to `shared=True`, which appends a finalizer to a
    # *module-level* list -- the same mechanism `shared_task` uses -- so every
    # application finalized afterwards re-creates this job on itself. A test
    # registering one job would leak it into the worker's application, and a
    # worker would advertise jobs from whatever else had been imported.
    #
    # That is the ambient global `set_as_current=False` was chosen to avoid,
    # arriving through a different door. Found by the test asserting that two
    # applications do not share a registry.
    return app.task(run, name=name, bind=True, shared=False)


def _inbound_correlation_id(task: Any) -> str | None:
    """Return the correlation id the message carried, if it carried one.

    A job invoked directly -- by a test, or by another job calling the function
    rather than the task -- has no request at all. That is not an error: the
    correlation already bound in this process is then the right one, and
    ``None`` leaves it alone rather than replacing it with a fresh one that
    would detach the job's records from the work that started it.
    """
    request = getattr(task, "request", None)
    headers = getattr(request, "headers", None) or {}
    inbound = headers.get(CORRELATION_HEADER)
    return str(inbound) if inbound else current_correlation_id()
