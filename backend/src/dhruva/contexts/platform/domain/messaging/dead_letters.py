"""When a dead letter may be re-queued (ADR-064).

Pure policy. No database, no clock, no transport -- the whole module is a
function of its arguments, so the rules can be stated and tested without one.

ADR-064 makes replay **explicit**: a dead letter is moved aside and stays there
until a human decides otherwise. That decision is not unconditional, and the two
conditions below are the ones that would otherwise be discovered in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["RequeueRefusal", "RequeueVerdict", "may_requeue"]


class RequeueRefusal(StrEnum):
    """Why a re-queue was refused. Named, so an operator is told rather than guessing."""

    ALREADY_PUBLISHED = "already_published"
    """The event reached the transport. Re-queueing would publish it a second
    time -- deliberately, rather than as the accident at-least-once permits, and
    with nothing recording that a human chose to."""

    NOT_DEAD_LETTERED = "not_dead_lettered"
    """The event is still being retried. Re-queueing would reset its attempt
    budget, so a message failing every time would never exhaust one and would
    never be moved aside -- which is the exact loop ADR-064 exists to end."""


@dataclass(frozen=True, slots=True)
class RequeueVerdict:
    """Whether a row may return to the delivery queue, and why not if it may not."""

    permitted: bool
    refusal: RequeueRefusal | None = None

    def __post_init__(self) -> None:
        """Refuse a verdict that says both or neither."""
        if self.permitted is (self.refusal is not None):
            message = "a verdict must either permit or name a refusal, never both or neither"
            raise ValueError(message)


#: The one permitted verdict, shared rather than reconstructed per call.
PERMITTED = RequeueVerdict(permitted=True)


def may_requeue(*, published: bool, dead_lettered: bool) -> RequeueVerdict:
    """Decide whether a dead-lettered row may be returned to the queue.

    Parameters
    ----------
    published
        Whether the transport has already accepted this event.
    dead_lettered
        Whether the row has actually been moved aside.

    Notes
    -----
    Order matters. ``published`` is checked first because a published row that
    is *also* dead-lettered is the more alarming state -- it means something
    marked a delivered event as undeliverable -- and the operator should be told
    about the publication rather than about the dead-lettering.
    """
    if published:
        return RequeueVerdict(permitted=False, refusal=RequeueRefusal.ALREADY_PUBLISHED)
    if not dead_lettered:
        return RequeueVerdict(permitted=False, refusal=RequeueRefusal.NOT_DEAD_LETTERED)
    return PERMITTED
