"""Every platform error survives the worker boundary (ADR-066, ADR-038).

ADR-066's consequences require this test by name: a job runs in a worker
process, and an error it raises is serialised, shipped to whichever process is
watching, and reconstructed there. An error that does not survive that trip
arrives as something else -- usually a bare ``UnpicklingError`` naming nothing
useful -- and the failure that actually happened is gone.

Discovered rather than enumerated. A list of error classes written by hand is a
list that stops covering the taxonomy the first time somebody adds a class, and
the gap shows up as an unreadable traceback from a worker at three in the
morning. Walking the subclass tree means a new error is covered the moment it
exists.

Independent of Celery. Pickling is the standard library's, the taxonomy is the
platform's, and neither needs a broker to be exercised -- so this holds now,
before the executor is wired, rather than after.
"""

from __future__ import annotations

import pickle

import pytest

from dhruva.shared.errors import DhruvaError, ErrorCode

pytestmark = pytest.mark.unit

#: Context values chosen to cover what a real error carries: a string, an
#: integer, a nested structure, and ``None``. A payload of one string would pass
#: while a dict-valued context silently vanished.
CONTEXT = {
    "instrument_id": "NIFTY26JAN",
    "attempts": 3,
    "detail": {"reason": "broker refused", "codes": [1, 2, 3]},
    "previous": None,
}


def taxonomy() -> list[type[DhruvaError]]:
    """Return every concrete error class in the taxonomy, discovered by walking."""
    found: list[type[DhruvaError]] = []
    frontier = [DhruvaError]
    while frontier:
        current = frontier.pop()
        found.append(current)
        frontier.extend(current.__subclasses__())
    return sorted(set(found), key=lambda cls: cls.__name__)


ERRORS = taxonomy()


def test_the_taxonomy_is_actually_discovered() -> None:
    """A discovery that found nothing would make every test below vacuous.

    The floor is deliberate rather than exact: an exact count would fail on every
    new error class, which teaches people to edit the number rather than read the
    test.
    """
    names = {cls.__name__ for cls in ERRORS}

    assert len(ERRORS) >= 15
    assert {"ValidationError", "ConflictError", "UpstreamUnavailableError"} <= names


@pytest.mark.parametrize("error_class", ERRORS, ids=lambda cls: cls.__name__)
def test_an_error_survives_being_shipped_between_processes(
    error_class: type[DhruvaError],
) -> None:
    """Message, code and structured context all arrive intact.

    The context is what makes a worker's failure diagnosable -- which instrument,
    which attempt, which broker response. An error that arrived with its message
    and lost its context would look like it had been handled properly while
    telling nobody anything.
    """
    original = error_class("something went wrong", **CONTEXT)

    revived = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own bytes, not input

    assert type(revived) is error_class
    assert revived.message == original.message
    assert revived.context == CONTEXT
    assert revived.code == original.code
    assert str(revived) == str(original)


@pytest.mark.parametrize("error_class", ERRORS, ids=lambda cls: cls.__name__)
def test_an_error_with_no_context_survives_too(error_class: type[DhruvaError]) -> None:
    """The empty case, which travels a different path through ``__reduce__``."""
    revived = pickle.loads(pickle.dumps(error_class("bare")))  # noqa: S301 - our own bytes

    assert type(revived) is error_class
    assert revived.context == {}
    assert revived.message == "bare"


@pytest.mark.parametrize("error_class", ERRORS, ids=lambda cls: cls.__name__)
def test_a_raised_and_caught_error_still_survives(error_class: type[DhruvaError]) -> None:
    """Raising populates ``__traceback__``, which is *not* picklable.

    A worker never pickles an error it constructed and put down; it pickles one
    it caught. If the traceback came along, every error would fail to serialise
    at exactly the moment it mattered, and this test would be the only place that
    difference showed.
    """
    try:
        raise error_class("raised in anger", **CONTEXT)
    except DhruvaError as caught:
        revived = pickle.loads(pickle.dumps(caught))  # noqa: S301 - our own bytes

    assert type(revived) is error_class
    assert revived.context == CONTEXT


def test_the_error_code_is_preserved_rather_than_recomputed() -> None:
    """The code is what an operator greps for, and it must not change in transit."""
    original = DhruvaError("generic")

    revived = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own bytes

    assert isinstance(revived.code, ErrorCode)
    assert revived.code == original.code


def test_an_exception_chain_does_not_survive_the_trip() -> None:
    """Pinned as a *limitation*, because it is CPython's and not the platform's.

    ``BaseException.__reduce__`` carries the class, ``args`` and ``__dict__``.
    It does not carry ``__cause__``, ``__context__`` or ``__traceback__``, so a
    ``raise ... from ...`` chain is lost the moment an error crosses a process
    boundary. Nothing in this project can change that without overriding
    ``__reduce__`` -- and overriding it would make an error whose *cause* is
    unpicklable, such as a driver exception holding a socket, fail to serialise
    at all. Losing the chain is strictly better than losing the error.

    Asserted rather than left implicit so that the next person to look for the
    cause in a worker log finds this test instead of spending an afternoon on it,
    and so that a future CPython that does preserve it is noticed rather than
    silently relied upon.
    """
    try:
        try:
            raise ValueError("the underlying cause")  # noqa: TRY301 - the subject of the test
        except ValueError as cause:
            raise DhruvaError("the platform's account of it") from cause
    except DhruvaError as error:
        caught = error

    assert caught.__cause__ is not None, "the chain exists in this process"

    revived = pickle.loads(pickle.dumps(caught))  # noqa: S301 - our own bytes

    assert revived.__cause__ is None, "and does not exist in the next one"


def test_the_cause_survives_when_it_is_recorded_as_context() -> None:
    """Which is why every translation in the platform puts the cause in context.

    ``reason=str(error)`` in the Redis adapter's error mapping, in the ledger's
    duplicate, and in the envelope's decoder is not decoration. Given the test
    above, it is the *only* part of an underlying failure that reaches whoever is
    reading the worker's output -- and this asserts that the convention actually
    works rather than trusting that it does.
    """
    try:
        raise ValueError("connection reset by peer")  # noqa: TRY301 - the subject of the test
    except ValueError as cause:
        shipped = DhruvaError("the platform's account of it", reason=str(cause))

    revived = pickle.loads(pickle.dumps(shipped))  # noqa: S301 - our own bytes

    assert revived.context["reason"] == "connection reset by peer"
