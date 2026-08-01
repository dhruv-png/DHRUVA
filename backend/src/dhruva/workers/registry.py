"""What jobs exist, and which of them are scheduled (ADR-031, ADR-066).

One table, read at startup by the composition root. A job appears here or it does
not run: there is no autodiscovery, whose failure mode is a module in the wrong
place registering nothing while the worker starts cleanly and reports no error.

Declaration and installation are separate on purpose. :data:`REGISTRY` is data --
importable, printable, assertable, and free of Celery. :func:`install` is the
only part that touches an application, and it is called once, from the one place
permitted to build one.

A schedule needs a calendar
---------------------------
A job may declare *when* it runs, and that declaration resolves through a
:class:`TradingCalendar` (ADR-066). If a scheduled job is installed without one,
:func:`install` refuses rather than registering the task and quietly omitting its
beat entry. The quiet version is the worse failure by a distance: the job exists,
``celery inspect registered`` lists it, and it never once runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from dhruva.shared.errors import ConfigurationError
from dhruva.shared.logging import get_logger
from dhruva.workers.beat import TradingDayBeatSchedule
from dhruva.workers.tasks import register_job

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from celery import Celery

    from dhruva.contexts.platform.domain.scheduling import TradingDaySchedule
    from dhruva.shared.time import TradingCalendar

__all__ = ["REGISTRY", "JobDefinition", "install"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """One job: what it is called, what it does, and when it runs.

    Attributes
    ----------
    name
        The task name on the wire, validated by
        :func:`~dhruva.workers.tasks.register_job`.
    func
        The job. A plain function -- it imports no Celery and is tested by being
        called.
    schedule
        When it runs, or ``None`` for a job only ever enqueued by something else.
        Session-relative rather than a cron string, because a cron string is
        wrong on every exchange holiday and no test can contradict it (ADR-066).
    """

    name: str
    func: Callable[..., Any]
    schedule: TradingDaySchedule | None = None

    @property
    def is_scheduled(self) -> bool:
        """Return whether this job runs on a timetable of its own."""
        return self.schedule is not None


#: Every job the platform runs.
#:
#: Empty, and that is the current truth rather than a placeholder: S05 builds the
#: runtime, and the first job belongs to the subsystem that needs one. An empty
#: tuple installs cleanly, which is what makes the wiring below testable now
#: instead of on the day it first matters.
REGISTRY: Final[tuple[JobDefinition, ...]] = ()


def install(
    app: Celery,
    definitions: Sequence[JobDefinition] = REGISTRY,
    *,
    calendar: TradingCalendar | None = None,
) -> dict[str, Any]:
    """Register every job on ``app`` and return the tasks by name.

    Parameters
    ----------
    app
        The application from the composition root.
    definitions
        What to install. Defaults to :data:`REGISTRY`; a test supplies its own
        rather than mutating the module's.
    calendar
        Resolves the session-relative schedules. Required if any definition
        carries one, and unused otherwise -- a worker that runs no scheduled job
        needs no calendar, and demanding one would make it a dependency of
        processes that have nothing to do with the trading day.

    Returns
    -------
    dict[str, Any]
        The registered tasks, keyed by name, for a caller that needs to enqueue
        one.

    Raises
    ------
    ConfigurationError
        If two definitions share a name, or a scheduled job is installed with no
        calendar to resolve it against.

    Notes
    -----
    Beat entries are written onto ``app.conf.beat_schedule`` rather than returned,
    because that is where beat reads them and a second copy would be a second
    thing to keep in step.
    """
    _refuse_duplicates(definitions)

    tasks: dict[str, Any] = {}
    entries: dict[str, Any] = {}

    for definition in definitions:
        tasks[definition.name] = register_job(app, definition.func, name=definition.name)
        if definition.schedule is not None:
            entries[definition.name] = {
                "task": definition.name,
                "schedule": TradingDayBeatSchedule(
                    definition.schedule, _required_calendar(definition, calendar)
                ),
            }

    if entries:
        app.conf.beat_schedule = {**dict(app.conf.beat_schedule or {}), **entries}

    _log.info("jobs installed", count=len(tasks), scheduled=len(entries))
    return tasks


def _refuse_duplicates(definitions: Sequence[JobDefinition]) -> None:
    """Refuse a registry in which one name appears twice.

    Celery's registry is a dictionary, so the second definition would replace the
    first without a word -- and the job that stopped running would be the one
    nobody edited.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for definition in definitions:
        if definition.name in seen:
            duplicates.add(definition.name)
        seen.add(definition.name)

    if duplicates:
        raise ConfigurationError(
            "two jobs are registered under the same name; the second would "
            "silently replace the first",
            names=sorted(duplicates),
        )


def _required_calendar(
    definition: JobDefinition, calendar: TradingCalendar | None
) -> TradingCalendar:
    """Return the calendar, refusing to install a scheduled job without one."""
    if calendar is None:
        raise ConfigurationError(
            "a scheduled job needs a calendar to resolve its session-relative "
            "moment against (ADR-066); installing it without one would register "
            "the task and omit its schedule, so the job would exist and never run",
            job=definition.name,
            schedule=definition.schedule.describe() if definition.schedule else None,
        )
    return calendar
