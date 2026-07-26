"""Request-scoped correlation context.

Three identifiers travel with every unit of work and appear on every log record
and span emitted downstream (ADR-035, ADR-039):

``correlation_id``
    Identifies one logical operation end to end -- an HTTP request, a scheduled
    job run, a tick batch. Generated at the process edge if the caller did not
    supply one.
``causation_id``
    Identifies the *immediate cause*: the event or command that triggered this
    work. Together with ``correlation_id`` it turns a log archive into a causal
    graph, which is what makes an event-driven system debuggable at all.
``account_id``
    Present from the moment multi-tenancy activates (ADR-004). Bound now, unused
    now, so that no log line has to be retrofitted at S44.

Implementation uses :mod:`contextvars`, which propagate automatically across
``await`` boundaries and into tasks created from the current context -- but
**not** into threads or process pools. :func:`copy_context_into` exists for that
case, and its absence is invisible until someone reads a log line with no
identifier on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Final, ParamSpec, TypeVar

__all__ = [
    "CorrelationContext",
    "bind_correlation",
    "copy_context_into",
    "current_context",
    "current_correlation_id",
    "new_correlation_id",
]

P = ParamSpec("P")
R = TypeVar("R")

_CORRELATION_ID: ContextVar[str | None] = ContextVar("dhruva_correlation_id", default=None)
_CAUSATION_ID: ContextVar[str | None] = ContextVar("dhruva_causation_id", default=None)
_ACCOUNT_ID: ContextVar[str | None] = ContextVar("dhruva_account_id", default=None)

#: Prefix making a generated identifier recognisable in a mixed log archive.
_ID_PREFIX: Final = "dhv"


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """A snapshot of the identifiers currently bound.

    Attributes
    ----------
    correlation_id
        The end-to-end operation identifier, if bound.
    causation_id
        The immediate cause, if bound.
    account_id
        The tenant, if bound.
    """

    correlation_id: str | None
    causation_id: str | None
    account_id: str | None

    def as_log_fields(self) -> dict[str, str]:
        """Render the bound identifiers as log fields, omitting unset ones.

        Returns
        -------
        dict[str, str]
            Only the identifiers that are actually bound. Emitting
            ``correlation_id: null`` on every record of a process that never binds
            one is noise, and noise is what makes people stop reading logs.
        """
        fields = {
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "account_id": self.account_id,
        }
        return {key: value for key, value in fields.items() if value is not None}


def new_correlation_id() -> str:
    """Return a fresh correlation identifier.

    Returns
    -------
    str
        ``dhv-<uuid4 hex>``. UUID4 rather than a counter or a timestamp because
        identifiers are generated in several processes that share no state, and
        a collision would silently merge two unrelated operations.
    """
    return f"{_ID_PREFIX}-{uuid.uuid4().hex}"


def current_context() -> CorrelationContext:
    """Return the identifiers currently bound to this execution context."""
    return CorrelationContext(
        correlation_id=_CORRELATION_ID.get(),
        causation_id=_CAUSATION_ID.get(),
        account_id=_ACCOUNT_ID.get(),
    )


def current_correlation_id() -> str | None:
    """Return the bound correlation identifier, or ``None`` if there is none."""
    return _CORRELATION_ID.get()


@contextmanager
def bind_correlation(
    *,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    account_id: str | None = None,
    generate_missing: bool = True,
) -> Iterator[CorrelationContext]:
    """Bind correlation identifiers for the duration of the block.

    Parameters
    ----------
    correlation_id
        Supplied by the caller -- for example from an inbound ``X-Correlation-ID``
        header or an event envelope. Generated if absent and ``generate_missing``.
    causation_id
        The event or command that caused this work.
    account_id
        The tenant this work belongs to.
    generate_missing
        Whether to generate a ``correlation_id`` when none is supplied and none is
        already bound. Defaults to ``True``: at a process edge, work without an
        identifier is work that cannot be traced.

    Yields
    ------
    CorrelationContext
        The identifiers now in force.

    Notes
    -----
    Values already bound are **inherited**, not cleared, when the corresponding
    argument is ``None``. Nested binds therefore narrow the context rather than
    resetting it -- an inner operation adding a ``causation_id`` keeps the outer
    ``correlation_id``, which is the behaviour that makes end-to-end tracing work.

    On exit, every variable is restored to its prior value. That restoration is
    what stops one request's identifiers leaking into the next on a reused worker.
    """
    existing = current_context()
    resolved_correlation = correlation_id or existing.correlation_id
    if resolved_correlation is None and generate_missing:
        resolved_correlation = new_correlation_id()

    tokens = (
        _CORRELATION_ID.set(resolved_correlation),
        _CAUSATION_ID.set(causation_id or existing.causation_id),
        _ACCOUNT_ID.set(account_id or existing.account_id),
    )
    try:
        yield current_context()
    finally:
        correlation_token, causation_token, account_token = tokens
        _ACCOUNT_ID.reset(account_token)
        _CAUSATION_ID.reset(causation_token)
        _CORRELATION_ID.reset(correlation_token)


def copy_context_into(func: Callable[P, R]) -> Callable[P, R]:  # noqa: UP047
    """Wrap ``func`` so it runs with a copy of the caller's context.

    Needed whenever work crosses into a thread or a process pool, where
    :mod:`contextvars` do **not** propagate on their own.

    Parameters
    ----------
    func
        The callable to be executed elsewhere.

    Returns
    -------
    Callable
        A wrapper that captures the context at wrap time and runs ``func`` inside
        it.

    Examples
    --------
    >>> from concurrent.futures import ThreadPoolExecutor
    >>> def work() -> str | None:
    ...     return current_correlation_id()
    >>> with bind_correlation(correlation_id="dhv-abc"):
    ...     with ThreadPoolExecutor(max_workers=1) as pool:
    ...         pool.submit(copy_context_into(work)).result()
    'dhv-abc'

    Notes
    -----
    The ``UP047`` suppression on this signature is a **verification-environment
    constraint, not a style preference**. PEP 695 syntax (``def
    copy_context_into[**P, R]``) is correct for the Python 3.12 target, but the
    current verification interpreter is 3.10 and cannot parse it -- adopting it
    would mean shipping this module without running the test suite or the type
    checker over it at all, which is a far worse trade than one suppressed rule.
    Tracked in the S02 release notes; remove the suppression once a 3.12
    interpreter is available.
    """
    context = copy_context()

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return context.run(func, *args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "wrapped")
    wrapper.__doc__ = func.__doc__
    return wrapper
