"""Render archived news and polling outcomes for a human, with attribution.

Pure text. No client, no session, no store -- the presentation of a stored fact
is testable without any of them, and a future dashboard should be able to reuse
these decisions rather than reinvent them in a template.

Attribution is not decoration here. GDELT's terms permit unrestricted use *on
condition of* a citation and a link back, so a rendering that omits them is not
a cosmetic lapse. It cannot happen by accident either: ``NewsSource`` refuses to
exist without a canonical homepage URL, so every item that reached the archive
carries somewhere to point, and :func:`render_item` always prints it.

The other thing every rendering says out loud is what is *not* here. NSE filings
are not a DHRUVA input, and a news list that looks complete is worse than one
that admits what it excludes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import MatchState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.contexts.intelligence.application.news_polling import (
        BatchOutcome,
        PollNewsFeedsResult,
    )
    from dhruva.contexts.intelligence.domain.archive import ArchivedNewsItem
    from dhruva.contexts.intelligence.domain.search import SearchPlan

__all__ = [
    "NSE_UNAVAILABLE_NOTICE",
    "render_batch_outcomes",
    "render_ingestion_counts",
    "render_item",
    "render_items",
    "render_plan",
]

#: Printed by every command. The absence of exchange filings is a property of
#: the product, not an outage, and an operator who does not know that will read
#: a quiet news list as "nothing was announced".
NSE_UNAVAILABLE_NOTICE = (
    "NSE filings and exchange announcements are NOT an input to DHRUVA. "
    "Automated NSE ingestion is deferred pending written permission or a "
    "licensed data agreement (docs/decisions/news-source-selection.md)."
)

#: Revision hashes are 64 hex characters. Eight is enough to tell two revisions
#: of one headline apart in a terminal, and the full value is in the database
#: for anyone who needs to join on it.
_REVISION_PREFIX = 8
_INDENT = "    "


def render_item(item: ArchivedNewsItem) -> str:
    """Render one archived revision, with everything needed to cite it."""
    news = item.revision.item
    lines = [
        f"{news.published_at.isoformat()}  {news.text.title}",
        f"{_INDENT}url        : {news.identity.url}",
        f"{_INDENT}source     : {news.source.display_name} [{news.source.tier}]",
        f"{_INDENT}attribution: {news.source.key} -- {news.source.homepage_url}",
        f"{_INDENT}published   (as claimed by the source): {news.published_at.isoformat()}",
        f"{_INDENT}first seen  (by DHRUVA)              : {news.first_seen_at.isoformat()}",
        f"{_INDENT}revision   : {item.revision.revision[:_REVISION_PREFIX]}"
        f"  item id: {news.identity.provider_item_id}",
    ]
    decision = item.revision.deduplication
    if decision.is_duplicate:
        original = "an earlier item" if decision.original is None else decision.original.url
        lines.append(f"{_INDENT}duplicate  : {decision.rule} of {original}")
    lines.extend(_analysis_lines(item))
    if news.text.snippet is not None:
        lines.append(f"{_INDENT}snippet    : {news.text.snippet}")
    return "\n".join(lines)


def _analysis_lines(item: ArchivedNewsItem) -> list[str]:
    """Render the deterministic rulesets' verdicts, or say they did not run."""
    analysis = item.analysis
    if analysis is None:
        return [f"{_INDENT}analysis   : none stored for this revision"]
    sentiment = analysis.sentiment
    detail = f"{sentiment.label} (score {sentiment.score}, confidence {sentiment.confidence})"
    if sentiment.abstention_reason is not None:
        detail = f"{sentiment.label} (abstained: {sentiment.abstention_reason})"
    lines = [
        f"{_INDENT}event      : {analysis.event.category}",
        f"{_INDENT}sentiment  : {detail}",
    ]
    linked = analysis.linked
    if not linked:
        lines.append(f"{_INDENT}instruments: none matched")
        return lines
    for match, state in linked:
        marker = "" if state is MatchState.MATCHED else f" [{state}]"
        lines.append(
            f"{_INDENT}instrument : {match.canonical_symbol}{marker}"
            f"  matched {match.kind} on {match.matched_text!r}"
        )
    return lines


def render_items(items: Sequence[ArchivedNewsItem]) -> str:
    """Render a point-in-time result, saying plainly when it is empty."""
    if not items:
        return "no archived news matched this instrument at this cutoff"
    return "\n\n".join(render_item(item) for item in items)


def render_plan(plan: SearchPlan) -> str:
    """Render what a pass intends to ask for, and what it gave up on."""
    lines = [
        f"search plan: {plan.revision}  {plan.phrase_count} phrases in {len(plan.batches)} batches"
    ]
    for batch in plan.batches:
        lines.append(f"{_INDENT}batch {batch.index}: {', '.join(batch.texts)}")
    for refusal in plan.rejected:
        lines.append(
            f"{_INDENT}not queried: {refusal.text!r} for {refusal.canonical_symbol} "
            f"({refusal.kind}) -- {refusal.reason}"
        )
    for entry in plan.unqueryable:
        lines.append(f"{_INDENT}UNQUERYABLE: {entry.canonical_symbol} -- {entry.reason}")
    return "\n".join(lines)


def render_batch_outcomes(outcomes: Sequence[BatchOutcome]) -> str:
    """Render one line per batch, including the ones never issued."""
    lines = []
    for outcome in outcomes:
        if outcome.status is None:
            lines.append(
                f"{_INDENT}batch {outcome.index}: NOT ISSUED -- the pass stopped before this batch"
            )
            continue
        status = outcome.status
        http = "" if status.http_status is None else f" http {status.http_status}"
        retry = (
            ""
            if status.retry_after is None
            else f" retry-after {status.retry_after.total_seconds():.0f}s"
        )
        lines.append(
            f"{_INDENT}batch {outcome.index}: {status.health}{http}{retry} "
            f"-- {status.reason} ({outcome.items_offered} items)"
        )
    return "\n".join(lines)


def render_ingestion_counts(result: PollNewsFeedsResult) -> str:
    """Render what ingestion did with the items the healthy batches returned.

    The counts are the ones the ingestion result actually carries. There is no
    separate "revised" total, because a correction is stored as a new revision
    and is therefore already inside ``inserted``; and "rejected" is reported as
    ``duplicates``, which is what the deduplication ledger actually decided.
    """
    return "\n".join(
        (
            f"{_INDENT}fetched    : {result.items_offered}",
            f"{_INDENT}inserted   : {result.revisions_added} "
            "(a correction arrives here, as a new revision)",
            f"{_INDENT}unchanged  : {result.revisions_unchanged}",
            f"{_INDENT}duplicates : {result.duplicates}",
            f"{_INDENT}analyses   : {result.analyses_added}",
            f"{_INDENT}links      : {result.links_added}",
            f"{_INDENT}unresolved : {result.unresolved} (matched no approved instrument)",
        )
    )
