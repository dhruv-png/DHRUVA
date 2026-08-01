"""When a dead letter may be re-queued (ADR-064).

Pure policy, tested as pure policy. Both refusals below describe a state that
would otherwise be discovered in production, by an operator who thought they had
fixed something.
"""

from __future__ import annotations

import pytest

from dhruva.contexts.platform.domain.messaging import (
    RequeueRefusal,
    RequeueVerdict,
    may_requeue,
)

pytestmark = pytest.mark.unit


def test_a_dead_lettered_unpublished_event_may_return_to_the_queue() -> None:
    """The case the command exists for."""
    assert may_requeue(published=False, dead_lettered=True).permitted


def test_a_published_event_is_refused() -> None:
    """Re-queueing it would publish a second copy deliberately.

    At-least-once permits a duplicate as the price of never losing an event. It
    does not licence manufacturing one on purpose, with nothing recording that a
    human chose to.
    """
    verdict = may_requeue(published=True, dead_lettered=True)

    assert not verdict.permitted
    assert verdict.refusal is RequeueRefusal.ALREADY_PUBLISHED


def test_an_event_still_being_retried_is_refused() -> None:
    """Re-queueing it would reset its attempt budget.

    A message failing every time would then never exhaust one and would never be
    moved aside -- the exact infinite loop ADR-064 exists to end, reintroduced by
    the tool meant to recover from it.
    """
    verdict = may_requeue(published=False, dead_lettered=False)

    assert not verdict.permitted
    assert verdict.refusal is RequeueRefusal.NOT_DEAD_LETTERED


def test_publication_is_reported_before_dead_lettering() -> None:
    """A published *and* dead-lettered row is the more alarming state.

    It means something marked a delivered event as undeliverable, and the
    operator needs to be told about the publication rather than about the
    dead-lettering.
    """
    verdict = may_requeue(published=True, dead_lettered=False)

    assert verdict.refusal is RequeueRefusal.ALREADY_PUBLISHED


@pytest.mark.parametrize(
    ("permitted", "refusal"),
    [(True, RequeueRefusal.ALREADY_PUBLISHED), (False, None)],
)
def test_a_verdict_must_permit_or_refuse_but_never_both_or_neither(
    permitted: bool, refusal: RequeueRefusal | None
) -> None:
    """A verdict that says both is a verdict a caller will read wrongly."""
    with pytest.raises(ValueError, match="permit or name a refusal"):
        RequeueVerdict(permitted=permitted, refusal=refusal)
