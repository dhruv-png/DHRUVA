"""Installing the job registry (ADR-031, ADR-066).

The registry is data and the installation is the only part that touches an
application, so most of what matters here is assertable without one: what a
definition says, what a duplicate does, and what happens to a scheduled job with
nowhere to resolve its schedule.

Nothing connects to anything, and nothing sleeps.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from celery import Celery

from dhruva.contexts.platform.domain.scheduling import SessionAnchor, TradingDaySchedule
from dhruva.shared.config.settings import RedisSettings
from dhruva.shared.errors import ConfigurationError, ValidationError
from dhruva.workers.beat import TradingDayBeatSchedule
from dhruva.workers.celery_app import build_celery_app
from dhruva.workers.registry import REGISTRY, JobDefinition, install
from tests.unit.scheduling.test_trading_day_schedule import CALENDAR

pytestmark = pytest.mark.unit

AT_OPEN = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, offset=timedelta(minutes=5))


def noop() -> None:
    """Do nothing, verifiably."""


def prune() -> str:
    """Return something, so a caller can tell it ran."""
    return "pruned"


@pytest.fixture
def app() -> Celery:
    """Build an application with no broker behind it."""
    return build_celery_app(RedisSettings())


# --------------------------------------------------------------------------- #
# What the registry says today
# --------------------------------------------------------------------------- #


def test_the_shipped_registry_is_empty_and_installs_cleanly(app: Celery) -> None:
    """Empty is the current truth, not a placeholder.

    S05 builds the runtime; the first job belongs to the subsystem that needs
    one. Asserting that an empty registry installs is what makes the wiring
    testable now rather than on the day it first matters.
    """
    assert REGISTRY == ()
    assert install(app) == {}
    assert dict(app.conf.beat_schedule or {}) == {}


def test_a_definition_knows_whether_it_is_scheduled() -> None:
    """The distinction a caller acts on, so it is named rather than inferred."""
    assert not JobDefinition(name="dhruva.prune", func=prune).is_scheduled
    assert JobDefinition(name="dhruva.prune", func=prune, schedule=AT_OPEN).is_scheduled


def test_a_definition_is_immutable() -> None:
    """A registry a caller can edit at runtime is a registry two processes disagree about."""
    definition = JobDefinition(name="dhruva.prune", func=prune)

    with pytest.raises(AttributeError):
        definition.name = "dhruva.something_else"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Installation
# --------------------------------------------------------------------------- #


def test_every_job_is_registered_and_returned(app: Celery) -> None:
    """The caller gets the tasks back, because enqueueing one needs the task."""
    tasks = install(
        app,
        [
            JobDefinition(name="dhruva.prune", func=prune),
            JobDefinition(name="dhruva.noop", func=noop),
        ],
    )

    assert sorted(tasks) == ["dhruva.noop", "dhruva.prune"]
    assert "dhruva.prune" in app.tasks
    assert tasks["dhruva.prune"]() == "pruned"


def test_an_unusable_job_name_is_refused_at_installation(app: Celery) -> None:
    """The name rule lives in ``register_job``; this asserts installation honours it.

    Catching it here rather than at first enqueue means a bad name fails the
    worker at startup, where somebody is watching.
    """
    with pytest.raises(ValidationError):
        install(app, [JobDefinition(name="prune", func=prune)])


def test_two_jobs_with_the_same_name_are_refused(app: Celery) -> None:
    """Celery's registry is a dictionary, so the second would replace the first.

    Silently -- and the job that stopped running would be the one nobody edited.
    """
    with pytest.raises(ConfigurationError, match="same name"):
        install(
            app,
            [
                JobDefinition(name="dhruva.prune", func=prune),
                JobDefinition(name="dhruva.prune", func=noop),
            ],
        )


def test_nothing_is_registered_when_a_duplicate_is_refused(app: Celery) -> None:
    """The refusal comes first, so a rejected registry leaves no half-installed worker."""
    with pytest.raises(ConfigurationError):
        install(
            app,
            [
                JobDefinition(name="dhruva.prune", func=prune),
                JobDefinition(name="dhruva.prune", func=noop),
            ],
        )

    assert "dhruva.prune" not in app.tasks


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


def test_a_scheduled_job_gets_a_beat_entry(app: Celery) -> None:
    """Written onto ``app.conf.beat_schedule``, which is where beat reads them."""
    install(
        app,
        [JobDefinition(name="dhruva.prune", func=prune, schedule=AT_OPEN)],
        calendar=CALENDAR,
    )

    entry = dict(app.conf.beat_schedule)["dhruva.prune"]
    assert entry["task"] == "dhruva.prune"
    assert isinstance(entry["schedule"], TradingDayBeatSchedule)
    assert repr(entry["schedule"]) == "<trading-day: market_open + 0:05:00>"


def test_an_unscheduled_job_gets_no_beat_entry(app: Celery) -> None:
    """A job enqueued by something else must not also run on a timetable."""
    install(app, [JobDefinition(name="dhruva.prune", func=prune)], calendar=CALENDAR)

    assert dict(app.conf.beat_schedule or {}) == {}


def test_a_scheduled_job_without_a_calendar_is_refused(app: Celery) -> None:
    """The failure this refusal replaces is far worse than a startup error.

    Registering the task and omitting its schedule leaves a job that exists,
    that ``celery inspect registered`` lists, and that never once runs.
    """
    with pytest.raises(ConfigurationError, match="calendar"):
        install(app, [JobDefinition(name="dhruva.prune", func=prune, schedule=AT_OPEN)])


def test_the_refusal_names_the_job_and_its_schedule(app: Celery) -> None:
    """An operator reading it must not have to search for which job was at fault."""
    with pytest.raises(ConfigurationError) as caught:
        install(app, [JobDefinition(name="dhruva.prune", func=prune, schedule=AT_OPEN)])

    assert caught.value.context["job"] == "dhruva.prune"
    assert caught.value.context["schedule"] == "market_open + 0:05:00"


def test_an_unscheduled_registry_needs_no_calendar(app: Celery) -> None:
    """A worker running no scheduled job must not require a trading calendar."""
    assert install(app, [JobDefinition(name="dhruva.prune", func=prune)]) != {}


def test_installing_twice_does_not_discard_the_first_set_of_entries(app: Celery) -> None:
    """Entries are merged, not replaced.

    A second call replacing the dictionary would silently unschedule everything
    installed before it -- and the worker would still start.
    """
    install(
        app, [JobDefinition(name="dhruva.prune", func=prune, schedule=AT_OPEN)], calendar=CALENDAR
    )
    install(
        app, [JobDefinition(name="dhruva.noop", func=noop, schedule=AT_OPEN)], calendar=CALENDAR
    )

    assert sorted(dict(app.conf.beat_schedule)) == ["dhruva.noop", "dhruva.prune"]
