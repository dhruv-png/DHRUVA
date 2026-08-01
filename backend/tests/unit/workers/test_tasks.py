"""Registering a job (ADR-031, ADR-033, ADR-038).

The job functions here are plain functions, which is the property under test as
much as anything else: if registering one required a Celery type, these tests
would need a broker to say anything.

Nothing connects to anything. Registration is bookkeeping on an application
object, and calling a task directly runs it in this process.
"""

from __future__ import annotations

import pytest
from celery import Celery
from structlog.testing import capture_logs

from dhruva.shared.config.settings import RedisSettings
from dhruva.shared.context import bind_correlation, current_correlation_id
from dhruva.shared.errors import UpstreamUnavailableError, ValidationError
from dhruva.workers.celery_app import build_celery_app
from dhruva.workers.tasks import CORRELATION_HEADER, JOB_NAME_PREFIX, register_job

pytestmark = pytest.mark.unit

JOB_NAME = "dhruva.reconcile_positions"


@pytest.fixture
def app() -> Celery:
    """Build an application with no broker behind it."""
    return build_celery_app(RedisSettings())


def test_a_plain_function_becomes_a_task(app: Celery) -> None:
    """The job takes no Celery types, and gains none by being registered."""

    def reconcile(count: int) -> int:
        return count * 2

    task = register_job(app, reconcile, name=JOB_NAME)

    assert task.name == JOB_NAME
    assert task(21) == 42


def test_the_task_is_registered_on_the_application_it_was_given(app: Celery) -> None:
    """``shared_task`` would resolve through a current application nobody configured."""

    def noop() -> None:
        return None

    register_job(app, noop, name=JOB_NAME)

    assert JOB_NAME in app.tasks


def test_two_applications_do_not_share_a_registry(app: Celery) -> None:
    """A test's application must not leak jobs into a worker's."""

    def noop() -> None:
        return None

    register_job(app, noop, name=JOB_NAME)
    other = build_celery_app(RedisSettings())

    assert JOB_NAME not in other.tasks


def test_the_job_keeps_its_identity(app: Celery) -> None:
    """A wrapper that renamed the function would make every traceback read wrongly."""

    def reconcile() -> None:
        """Reconcile the day's positions."""

    task = register_job(app, reconcile, name=JOB_NAME)

    assert task.__name__ == "reconcile"
    assert task.__doc__ == "Reconcile the day's positions."


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["dhruva.prune", "dhruva.ledger.prune", "dhruva.backfill_marketdata", "dhruva.a.b.c"],
)
def test_an_ordinary_job_name_is_accepted(app: Celery, name: str) -> None:
    """Lowercase, dot-separated, prefixed."""

    def noop() -> None:
        return None

    assert register_job(app, noop, name=name).name == name


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("reconcile", "an unprefixed name is one another application can also register"),
        ("celery.backend_cleanup", "a foreign prefix is another system's namespace"),
        ("dhruva.Reconcile", "case differences are invisible in a log field"),
        ("dhruva.reconcile positions", "a space breaks every line-oriented tool"),
        ("dhruva.", "a bare prefix names nothing"),
        ("dhruva.reconcile.", "a trailing dot is a segment that was forgotten"),
        ("", "an empty name is not a name"),
    ],
)
def test_an_unusable_job_name_is_refused(app: Celery, name: str, why: str) -> None:
    """The broker is a shared Redis, and a collision presents as work vanishing."""

    def noop() -> None:
        return None

    with pytest.raises(ValidationError):
        register_job(app, noop, name=name)
    assert why


def test_the_prefix_is_what_the_names_are_checked_against() -> None:
    """The constant and the rule must not drift apart."""
    assert JOB_NAME_PREFIX == "dhruva."
    assert JOB_NAME.startswith(JOB_NAME_PREFIX)


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


def test_a_job_runs_under_the_correlation_the_message_carried(app: Celery) -> None:
    """Every record the job emits must join up with the work that queued it.

    Without this, a job's logs are an island: something failed at 09:16 and
    nothing connects it to the request, event or schedule that caused it.
    """
    seen: list[str | None] = []

    def observe() -> None:
        seen.append(current_correlation_id())

    task = register_job(app, observe, name=JOB_NAME)

    with task.app.producer_or_acquire():
        task.push_request(headers={CORRELATION_HEADER: "dhv-from-the-message"})
        try:
            task()
        finally:
            task.pop_request()

    assert seen == ["dhv-from-the-message"]


def test_a_job_called_directly_keeps_the_correlation_already_bound(app: Celery) -> None:
    """A test, or a job calling another job's function, has no message at all.

    Replacing the bound id with a fresh one would detach the job's records from
    the work that started it -- which is the opposite of what correlation is for.
    """
    seen: list[str | None] = []

    def observe() -> None:
        seen.append(current_correlation_id())

    task = register_job(app, observe, name=JOB_NAME)

    with bind_correlation(correlation_id="dhv-already-here"):
        task()

    assert seen == ["dhv-already-here"]


# --------------------------------------------------------------------------- #
# Failure
# --------------------------------------------------------------------------- #


def test_a_failing_job_raises_what_it_raised(app: Celery) -> None:
    """Unchanged, deliberately.

    Wrapping would hand Celery's retry and failure handling a type the job never
    raised, and would discard the original in favour of a summary of it.
    """

    def explode() -> None:
        raise UpstreamUnavailableError("the broker is down", reason="connection refused")

    task = register_job(app, explode, name=JOB_NAME)

    with pytest.raises(UpstreamUnavailableError) as caught:
        task()

    assert caught.value.context["reason"] == "connection refused"


def test_a_failure_is_recorded_before_it_crosses_the_process_boundary(app: Celery) -> None:
    """The cause and the traceback exist in this process and in no other.

    ``BaseException.__reduce__`` carries neither, so an observer that logged the
    error itself would log it stripped of the chain that explains it. This is
    what makes that chain recoverable.

    ``exc_info`` on the record is the assertion that matters: it is what carries
    the traceback into the sink, and it is present only because the failure is
    logged with ``.exception()`` rather than described with ``.error()``.
    """

    def explode() -> None:
        msg = "the underlying cause"
        raise RuntimeError(msg)

    task = register_job(app, explode, name=JOB_NAME)

    with capture_logs() as records, pytest.raises(RuntimeError, match="the underlying cause"):
        task()

    [failure] = [record for record in records if record["event"] == "job failed"]
    assert failure["job"] == JOB_NAME
    assert failure["log_level"] == "error"
    assert failure["exc_info"] is True


def test_an_ordinary_job_logs_no_failure(app: Celery) -> None:
    """A log line that appears on every run is a log line nobody reads."""

    def succeed() -> str:
        return "done"

    task = register_job(app, succeed, name=JOB_NAME)

    with capture_logs() as records:
        assert task() == "done"

    assert [record for record in records if record["event"] == "job failed"] == []
