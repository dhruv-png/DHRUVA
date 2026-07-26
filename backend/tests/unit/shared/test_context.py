"""Correlation context binds, nests, restores, and does not leak between tasks.

The leak test matters most: a worker that carries one request's correlation ID
into the next produces a log archive that is confidently wrong, which is worse
than one with no identifiers at all.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from dhruva.shared.context import (
    bind_correlation,
    copy_context_into,
    current_context,
    current_correlation_id,
    new_correlation_id,
)


@pytest.mark.unit
def test_nothing_is_bound_by_default() -> None:
    """A process that has not reached an edge has no operation in flight."""
    assert current_correlation_id() is None
    assert current_context().as_log_fields() == {}


@pytest.mark.unit
def test_generated_ids_are_prefixed_and_unique() -> None:
    """The prefix makes an identifier recognisable in a mixed log archive."""
    first, second = new_correlation_id(), new_correlation_id()

    assert first.startswith("dhv-")
    assert first != second


@pytest.mark.unit
def test_binding_generates_an_id_when_none_is_supplied() -> None:
    """At a process edge, work without an identifier cannot be traced."""
    with bind_correlation() as context:
        assert context.correlation_id is not None
        assert current_correlation_id() == context.correlation_id


@pytest.mark.unit
def test_generation_can_be_declined() -> None:
    """Library code may want to observe context without inventing one."""
    with bind_correlation(generate_missing=False) as context:
        assert context.correlation_id is None


@pytest.mark.unit
def test_a_supplied_id_is_honoured() -> None:
    """Inbound headers and event envelopes carry the id that must be preserved."""
    with bind_correlation(correlation_id="dhv-inbound") as context:
        assert context.correlation_id == "dhv-inbound"


@pytest.mark.unit
def test_context_is_restored_on_exit() -> None:
    """Restoration is what stops one request's ids leaking into the next."""
    with bind_correlation(correlation_id="dhv-outer"):
        with bind_correlation(correlation_id="dhv-inner"):
            assert current_correlation_id() == "dhv-inner"
        assert current_correlation_id() == "dhv-outer"

    assert current_correlation_id() is None


@pytest.mark.unit
def test_context_is_restored_even_when_the_block_raises() -> None:
    """An exception must not strand a stale identifier on the worker."""
    with pytest.raises(RuntimeError), bind_correlation(correlation_id="dhv-x"):
        raise RuntimeError("boom")

    assert current_correlation_id() is None


@pytest.mark.unit
def test_nesting_narrows_rather_than_resets() -> None:
    """An inner bind adding causation keeps the outer correlation.

    This inheritance is what makes end-to-end tracing work across layers.
    """
    with (
        bind_correlation(correlation_id="dhv-root", account_id="acct-1"),
        bind_correlation(causation_id="evt-77"),
    ):
        context = current_context()

    assert context.correlation_id == "dhv-root"
    assert context.causation_id == "evt-77"
    assert context.account_id == "acct-1"


@pytest.mark.unit
def test_log_fields_omit_unbound_identifiers() -> None:
    """Emitting nulls on every record is noise, and noise stops people reading."""
    with bind_correlation(correlation_id="dhv-1"):
        assert current_context().as_log_fields() == {"correlation_id": "dhv-1"}

    with bind_correlation(correlation_id="dhv-1", causation_id="e", account_id="a"):
        assert set(current_context().as_log_fields()) == {
            "correlation_id",
            "causation_id",
            "account_id",
        }


@pytest.mark.unit
def test_context_propagates_across_await() -> None:
    """Context variables follow the coroutine, which is the common case."""

    async def inner() -> str | None:
        await asyncio.sleep(0)
        return current_correlation_id()

    async def outer() -> str | None:
        with bind_correlation(correlation_id="dhv-async"):
            return await inner()

    assert asyncio.run(outer()) == "dhv-async"


@pytest.mark.unit
def test_concurrent_tasks_do_not_leak_into_each_other() -> None:
    """Each task binds its own context; interleaving must not mix them.

    This is the failure that produces a confidently wrong log archive.
    """

    async def task(name: str) -> str | None:
        with bind_correlation(correlation_id=f"dhv-{name}"):
            await asyncio.sleep(0.01)
            return current_correlation_id()

    async def run() -> list[str | None]:
        return list(await asyncio.gather(*(task(n) for n in ("a", "b", "c"))))

    assert asyncio.run(run()) == ["dhv-a", "dhv-b", "dhv-c"]


@pytest.mark.unit
def test_context_does_not_reach_a_thread_without_help() -> None:
    """Documents the real limitation, so its absence is not a surprise later."""

    def read() -> str | None:
        return current_correlation_id()

    with bind_correlation(correlation_id="dhv-thread"), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(read).result() is None


@pytest.mark.unit
def test_copy_context_into_carries_it_across_a_thread() -> None:
    """The sanctioned wrapper for work handed to a pool."""

    def read() -> str | None:
        return current_correlation_id()

    with bind_correlation(correlation_id="dhv-thread"), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(copy_context_into(read)).result() == "dhv-thread"


@pytest.mark.unit
def test_the_wrapper_preserves_identity_for_debuggability() -> None:
    """A wrapper that erases the name makes every traceback less useful."""

    def named_worker() -> None:
        """Do nothing, informatively."""

    wrapped = copy_context_into(named_worker)

    assert wrapped.__name__ == "named_worker"
    assert wrapped.__doc__ == named_worker.__doc__
