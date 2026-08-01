"""The Celery application's configuration (ADR-062, ADR-066).

Each test pins one decision. Writing a default down is only worth doing if
something notices when it changes back, and these are what notice.

Nothing here connects to anything. Building the application is construction and
no more, which is what makes the whole of this a unit test.
"""

from __future__ import annotations

import pytest

from dhruva.shared.config import SecretValue
from dhruva.shared.config.settings import RedisSettings
from dhruva.workers.celery_app import (
    APP_NAME,
    DEFAULT_QUEUE,
    TASK_MODULES,
    VISIBILITY_TIMEOUT,
    build_celery_app,
)

pytestmark = pytest.mark.unit

#: Seconds kombu's Redis transport waits before re-queueing unacknowledged work
#: when nothing tells it otherwise. The number this suite exists to be clear of.
KOMBU_DEFAULT_VISIBILITY_TIMEOUT = 3600

#: A password containing every character that would re-parse as URL structure.
AWKWARD_PASSWORD = "p@ss:w/ord"  # noqa: S105 - a shape, not a credential


@pytest.fixture(autouse=True)
def _celery_environment_overrides_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variables Celery's own configuration reads behind the code.

    ``Settings.broker_url`` and ``Settings.result_backend`` consult
    ``os.environ`` before the configured value -- Celery reaching around the one
    module permitted to read the environment (boundary rule R5). Nothing can be
    done about that from here, but a developer's shell must not decide what this
    suite asserts.
    """
    for variable in ("CELERY_BROKER_URL", "CELERY_BROKER_READ_URL", "CELERY_RESULT_BACKEND"):
        monkeypatch.delenv(variable, raising=False)


def redis_settings(password: str | None = None) -> RedisSettings:
    """Build Redis settings whose every value differs from its default.

    Defaults that coincide with the values under test prove nothing: the
    assertion would pass against a function that ignored its argument.
    """
    secret = SecretValue(password, register=False) if password is not None else None
    return RedisSettings(host="redis.internal", port=6380, db=3, password=secret)


def test_the_broker_url_is_built_from_the_configured_server() -> None:
    """Host, port and database all come from settings, not from a default."""
    app = build_celery_app(redis_settings())

    assert app.conf.broker_url == "redis://redis.internal:6380/3"


def test_an_unauthenticated_broker_url_carries_no_credential_section() -> None:
    """Local development runs Redis without AUTH, and an empty ``:@`` is invalid."""
    app = build_celery_app(redis_settings())

    assert "@" not in app.conf.broker_url


def test_the_password_is_percent_encoded_into_the_broker_url() -> None:
    """A password containing URL structure must not become URL structure.

    Unencoded, ``p@ss:w/ord`` would parse as a host of ``ss``, and the connection
    would succeed against somewhere nobody intended rather than fail.
    """
    app = build_celery_app(redis_settings(password=AWKWARD_PASSWORD))

    assert app.conf.broker_url == "redis://:p%40ss%3Aw%2Ford@redis.internal:6380/3"


def test_the_password_does_not_reach_the_application_repr() -> None:
    """The broker URL is a credential; only Celery's connection code may hold it."""
    app = build_celery_app(redis_settings(password=AWKWARD_PASSWORD))

    assert AWKWARD_PASSWORD not in repr(app)


def test_a_worker_waits_for_a_broker_that_is_not_up_yet() -> None:
    """Container start order is the scheduler's business, not a reason to exit."""
    app = build_celery_app(redis_settings())

    assert app.conf.broker_connection_retry_on_startup is True


def test_only_json_crosses_the_broker() -> None:
    """Pickle over a broker is remote code execution, and Redis is not a trust boundary."""
    app = build_celery_app(redis_settings())

    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert list(app.conf.accept_content) == ["json"]


def test_time_is_utc_in_both_directions() -> None:
    """ADR-006. With ``enable_utc`` off, Celery hands beat a naive ``now``."""
    app = build_celery_app(redis_settings())

    assert app.conf.enable_utc is True
    assert app.conf.timezone == "UTC"


def test_a_job_is_acknowledged_after_it_runs_not_when_it_arrives() -> None:
    """ADR-062 applied to work: losing a job is worse than running one twice."""
    app = build_celery_app(redis_settings())

    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True


def test_a_worker_reserves_one_job_at_a_time() -> None:
    """The work here is backfills and backtests; Celery's default of four hoards them."""
    app = build_celery_app(redis_settings())

    assert app.conf.worker_prefetch_multiplier == 1


def test_the_visibility_timeout_outlasts_a_long_job() -> None:
    """Redelivery while the first copy still runs is the failure this number avoids."""
    app = build_celery_app(redis_settings())
    configured = app.conf.broker_transport_options["visibility_timeout"]

    assert configured == VISIBILITY_TIMEOUT.total_seconds()
    assert configured > KOMBU_DEFAULT_VISIBILITY_TIMEOUT


def test_no_result_backend_is_configured() -> None:
    """Outcomes are events and log lines. A store nobody reads is one nobody maintains."""
    app = build_celery_app(redis_settings())

    assert app.conf.result_backend is None
    assert app.conf.task_ignore_result is True


def test_celery_does_not_take_over_logging() -> None:
    """The root logger belongs to structlog, and the redaction processor is on that path."""
    app = build_celery_app(redis_settings())

    assert app.conf.worker_hijack_root_logger is False
    assert app.conf.worker_redirect_stdouts is False


def test_jobs_are_routed_to_a_named_queue() -> None:
    """Sharing Celery's default queue name on a shared Redis means eating another app's work."""
    app = build_celery_app(redis_settings())

    assert app.conf.task_default_queue == DEFAULT_QUEUE
    assert app.conf.task_default_queue != "celery"


def test_task_modules_are_declared_rather_than_discovered() -> None:
    """Autodiscovery's failure mode is a worker that starts cleanly and runs nothing."""
    app = build_celery_app(redis_settings())

    assert tuple(app.conf.imports) == TASK_MODULES


def test_no_jobs_are_registered_yet() -> None:
    """The application is wiring; the first job is a later commit and should look like one."""
    app = build_celery_app(redis_settings())

    assert [name for name in app.tasks if name.startswith(f"{APP_NAME}.")] == []


def test_the_application_is_not_made_the_current_one() -> None:
    """An ambient current app is the global ADR-031 removes.

    Its practical consequence is that a task must be registered with
    ``@app.task`` on the injected application; ``@shared_task`` resolves through
    whichever application is current and would find a default Celery built one.
    """
    app = build_celery_app(redis_settings())

    assert app.set_as_current is False


def test_each_call_returns_an_independent_application() -> None:
    """Two composition roots in one process -- a worker and a test -- must not share conf."""
    first = build_celery_app(redis_settings())
    second = build_celery_app(redis_settings(password=AWKWARD_PASSWORD))

    assert first is not second
    assert first.conf.broker_url != second.conf.broker_url


def test_the_application_is_named_for_the_system() -> None:
    """The name is what an operator sees in ``celery inspect``."""
    app = build_celery_app(redis_settings())

    assert app.main == APP_NAME
