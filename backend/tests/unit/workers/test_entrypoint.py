"""The worker composition root (ADR-031, ADR-033).

What is asserted here is *wiring*: that the one startup sequence runs, that
configuration reaches the application, and that nothing becomes ambient. The
Celery settings themselves are pinned next door in ``test_celery_app.py`` and
are not repeated.

Nothing connects to anything. ``create_app`` builds an application and a
runtime; neither opens a socket, which is what keeps this a unit test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from dhruva.shared.logging import logging_is_configured
from dhruva.workers.entrypoint import SERVICE_NAME, create_app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

#: Redis coordinates that differ from every default, so an assertion cannot pass
#: against an implementation that ignored configuration entirely.
REDIS_ENVIRONMENT = {
    "DHRUVA_REDIS__HOST": "redis.internal",
    "DHRUVA_REDIS__PORT": "6380",
    "DHRUVA_REDIS__DB": "3",
}


@pytest.fixture(autouse=True)
def _configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the Redis coordinates, and clear what Celery reads behind the code.

    ``Settings.broker_url`` consults ``os.environ`` before the configured value
    -- Celery reaching around the one module permitted to read the environment
    (boundary rule R5). Nothing can be done about that from here, but a
    developer's shell must not decide what this suite asserts.
    """
    for name, value in REDIS_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    for variable in ("CELERY_BROKER_URL", "CELERY_BROKER_READ_URL", "CELERY_RESULT_BACKEND"):
        monkeypatch.delenv(variable, raising=False)


def test_the_broker_is_configured_from_the_environment() -> None:
    """Settings reach the application, rather than the application inventing them.

    The coordinates asserted here are all non-default, so this fails against an
    implementation that built a Celery application and never looked at
    configuration at all.
    """
    app = create_app()

    assert app.conf.broker_url == "redis://redis.internal:6380/3"


def test_bringing_the_worker_up_configures_logging() -> None:
    """One startup sequence, shared with the API composition root.

    A worker whose logging was never configured would emit records that bypass
    the redaction processor (ADR-033, ADR-037) -- and would look completely
    normal while doing it.
    """
    create_app()

    assert logging_is_configured()


def test_the_worker_names_itself_distinctly_from_the_api() -> None:
    """The first question asked of a log line is which process wrote it."""
    assert SERVICE_NAME == "dhruva-worker"
    assert SERVICE_NAME != "dhruva-api"


def test_the_application_does_not_become_the_current_one() -> None:
    """An ambient current app is the global ADR-031 removes.

    Its practical consequence for a task author: register with ``@app.task`` on
    the application this returns, never with ``@shared_task``.
    """
    app = create_app()

    assert app.set_as_current is False


def test_each_call_returns_an_independent_application() -> None:
    """A worker and a beat in one process must not configure each other's."""
    first = create_app()
    second = create_app()

    assert first is not second
    assert first.conf is not second.conf


def test_no_beat_entries_are_registered_yet() -> None:
    """Deliberate, and asserted so that it is a decision rather than an oversight.

    A ``TradingDayBeatSchedule`` resolves through a ``TradingCalendar``, and no
    implementation of that port exists yet. Registering entries now would mean
    inventing a calendar to satisfy them, and a schedule resolved against an
    invented calendar runs on the wrong days while looking configured.
    """
    app = create_app()

    assert dict(app.conf.beat_schedule) == {}


def test_no_tasks_are_registered_yet() -> None:
    """The first job is a later commit and should read like one."""
    app = create_app()

    assert [name for name in app.tasks if name.startswith("dhruva.")] == []


def test_an_env_file_is_passed_through_to_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The argument exists so a deployment can supply one, so it must arrive.

    Asserted with the process environment *cleared* of the value under test:
    with it set, the file would be ignored -- environment variables win -- and
    this would pass whether or not the argument was ever forwarded.
    """
    monkeypatch.delenv("DHRUVA_REDIS__HOST", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DHRUVA_REDIS__HOST=from-the-file\n", encoding="utf-8")

    app = create_app(env_file)

    assert "from-the-file" in app.conf.broker_url


def test_the_process_environment_still_wins_over_a_file() -> None:
    """A file left in an image must not override what a deployment set."""
    app = create_app()

    assert "redis.internal" in app.conf.broker_url
