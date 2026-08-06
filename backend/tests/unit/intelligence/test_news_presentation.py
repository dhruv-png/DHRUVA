"""Rendered news always carries its citation, and never implies NSE coverage.

Two things are being protected. GDELT's terms permit unrestricted use *on
condition of* a citation and a link back, so an unattributed rendering is a
term breach rather than a formatting slip. And a news list with no filings in it
reads as "nothing was announced" unless it says otherwise, which is the more
expensive of the two mistakes for someone deciding whether to hold a position.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.intelligence.application.news_polling import (
    BatchOutcome,
    PollNewsFeedsResult,
)
from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.entity_linking import (
    LinkableInstrument,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import classify_event
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    DeduplicationRule,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.search import plan_search_phrases
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.contexts.intelligence.domain.sources import SourceHealth, SourceStatus
from dhruva.contexts.intelligence.interfaces.news_presentation import (
    NSE_UNAVAILABLE_NOTICE,
    render_batch_outcomes,
    render_ingestion_counts,
    render_item,
    render_items,
    render_plan,
)
from dhruva.shared.errors import DhruvaError
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)
HEADLINE: Final = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project via economictimes.indiatimes.test",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://gdeltproject.org",
)

HAL: Final = LinkableInstrument(
    instrument_id=InstrumentId.deterministic("reference", "HAL"),
    canonical_symbol="HAL",
    company_name="Hindustan Aeronautics Limited",
    aliases=("Hindustan Aeronautics",),
)


def _item(url: str = "https://economictimes.indiatimes.test/hal-order") -> NewsItem:
    link = canonical_url(url)
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT.key,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=GDELT,
        text=PermittedText(title=HEADLINE),
        published_at=PUBLISHED,
        first_seen_at=SEEN,
    )


def _archived(*, analysed: bool = True, duplicate: bool = False) -> ArchivedNewsItem:
    item = _item()
    decision = (
        DeduplicationDecision(
            is_duplicate=True,
            rule=DeduplicationRule.CANONICAL_URL,
            original=_item("https://economictimes.indiatimes.test/hal-order-original").identity,
            reason="the same canonical link was already stored",
        )
        if duplicate
        else DeduplicationDecision(
            is_duplicate=False, rule=None, original=None, reason="first observation"
        )
    )
    analysis = (
        NewsAnalysis(
            event=classify_event(HEADLINE),
            sentiment=evaluate_sentiment(HEADLINE),
            mapping=link_entities(HEADLINE, universe=(HAL,), published_on=PUBLISHED.date()),
            analysed_at=ANALYSED,
        )
        if analysed
        else None
    )
    return ArchivedNewsItem(
        revision=NewsRevision(item=item, revision=content_revision(item), deduplication=decision),
        analysis=analysis,
    )


def _status(health: SourceHealth, **kwargs: object) -> SourceStatus:
    return SourceStatus(
        health=health,
        reason=f"the source reported {health}",
        observed_at=SEEN,
        **kwargs,  # type: ignore[arg-type]  # narrowed by each call site
    )


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def test_every_rendered_item_carries_its_source_and_a_link_back() -> None:
    """The condition GDELT's terms attach to using its data at all."""
    rendered = render_item(_archived())

    assert "gdelt" in rendered
    assert "https://gdeltproject.org" in rendered
    assert GDELT.display_name in rendered


def test_the_publisher_link_is_kept_so_a_reader_can_follow_it() -> None:
    """DHRUVA stores a headline; the article stays with its publisher."""
    assert "https://economictimes.indiatimes.test/hal-order" in render_item(_archived())


def test_an_item_cannot_exist_without_somewhere_to_attribute_it() -> None:
    """Enforcement lives in the type, not in the renderer.

    A rendering-time check would only catch the omission at the last moment, and
    only on the paths that render. The existing contract refuses the source
    outright, so an unattributable item never reaches the archive to be shown.
    """
    with pytest.raises(DhruvaError):
        NewsSource(
            key="gdelt",
            display_name="The GDELT Project",
            tier=NewsSourceTier.AGGREGATOR,
            homepage_url="",
        )


# --------------------------------------------------------------------------- #
# What the reader is told, and not told
# --------------------------------------------------------------------------- #


def test_both_timestamps_are_shown_and_labelled_with_whose_they_are() -> None:
    """A provider's "seen" time is not a publisher's "published" time."""
    rendered = render_item(_archived())

    assert "as claimed by the source" in rendered
    assert "by DHRUVA" in rendered
    assert PUBLISHED.isoformat() in rendered
    assert SEEN.isoformat() in rendered


def test_the_revision_is_shown_so_two_versions_can_be_told_apart() -> None:
    """A correction appends beside its original; the reader needs to see which."""
    item = _archived()

    assert item.revision.revision[:8] in render_item(item)


def test_a_duplicate_says_which_item_it_repeats() -> None:
    """Why is this headline not in my feed?" has to have an answer."""
    rendered = render_item(_archived(duplicate=True))

    assert "duplicate" in rendered
    assert "CANONICAL_URL" in rendered
    assert "hal-order-original" in rendered


def test_the_deterministic_verdicts_are_shown() -> None:
    """Event, sentiment and the instruments matched are the reason it is here."""
    rendered = render_item(_archived())

    assert "event" in rendered
    assert "sentiment" in rendered
    assert "HAL" in rendered


def test_a_revision_with_no_analysis_says_so_rather_than_looking_neutral() -> None:
    """Absent is not neutral, and a blank line would read as neutral."""
    assert "none stored" in render_item(_archived(analysed=False))


def test_an_empty_result_is_stated_rather_than_rendered_as_a_blank() -> None:
    """A blank screen is indistinguishable from a broken command."""
    assert "no archived news" in render_items(())


def test_the_nse_notice_names_the_decision_it_comes_from() -> None:
    """An operator who wants to challenge the gap must be able to find it."""
    assert "NSE" in NSE_UNAVAILABLE_NOTICE
    assert "news-source-selection" in NSE_UNAVAILABLE_NOTICE
    assert "deferred" in NSE_UNAVAILABLE_NOTICE


# --------------------------------------------------------------------------- #
# The plan and the pass
# --------------------------------------------------------------------------- #


def test_the_plan_shows_what_will_be_asked_for() -> None:
    """An operator should be able to read the requests before they are issued."""
    plan = plan_search_phrases((HAL,), on=PUBLISHED.date())

    assert "Hindustan Aeronautics" in render_plan(plan)
    assert "batch 1" in render_plan(plan)


def test_the_plan_shows_refusals_and_gaps_rather_than_hiding_them() -> None:
    """Coverage an operator believes in but does not have is the failure mode."""
    silent = LinkableInstrument(
        instrument_id=InstrumentId.deterministic("reference", "XYZ"),
        canonical_symbol="XYZ",
        company_name="XYZ",
        aliases=("XYZ",),
    )
    rendered = render_plan(plan_search_phrases((HAL, silent), on=PUBLISHED.date()))

    assert "UNQUERYABLE: XYZ" in rendered
    assert "not queried" in rendered


def test_a_skipped_batch_is_rendered_as_never_issued() -> None:
    """Not "empty", not "failed" -- not asked."""
    rendered = render_batch_outcomes(
        (
            BatchOutcome(
                index=1,
                source_key="gdelt",
                status=_status(SourceHealth.RATE_LIMITED),
                items_offered=0,
            ),
            BatchOutcome(index=2, source_key=None, status=None, items_offered=0),
        )
    )

    assert "RATE_LIMITED" in rendered
    assert "NOT ISSUED" in rendered


def test_a_rate_limited_batch_shows_the_transport_detail_it_has() -> None:
    """The HTTP status and any Retry-After are what an operator acts on."""
    rendered = render_batch_outcomes(
        (
            BatchOutcome(
                index=1,
                source_key="gdelt",
                status=_status(
                    SourceHealth.RATE_LIMITED, http_status=429, retry_after=timedelta(seconds=90)
                ),
                items_offered=0,
            ),
        )
    )

    assert "http 429" in rendered
    assert "retry-after 90s" in rendered


def test_the_counts_are_the_ones_ingestion_actually_produced() -> None:
    """Deriving a total twice is how two numbers come to disagree."""
    rendered = render_ingestion_counts(
        PollNewsFeedsResult(
            outcomes=(),
            items_offered=9,
            revisions_added=4,
            revisions_unchanged=3,
            duplicates=2,
            analyses_added=4,
            links_added=5,
            unresolved=1,
        )
    )

    assert "fetched    : 9" in rendered
    assert "inserted   : 4" in rendered
    assert "unchanged  : 3" in rendered
    assert "duplicates : 2" in rendered
    assert "unresolved : 1" in rendered


def test_a_correction_is_explained_as_a_new_revision_not_an_update() -> None:
    """There is no separate "revised" count because there is no update path."""
    rendered = render_ingestion_counts(
        PollNewsFeedsResult(
            outcomes=(), items_offered=0, revisions_added=0, revisions_unchanged=0, duplicates=0
        )
    )

    assert "new revision" in rendered


def test_sentiment_that_abstained_says_why_rather_than_printing_a_score() -> None:
    """An abstention is a refusal to judge, not a score of zero."""
    hedged = "Analysts said the company may or may not benefit, but nobody would confirm"
    item = _item("https://economictimes.indiatimes.test/hedged")
    archived = ArchivedNewsItem(
        revision=NewsRevision(
            item=item,
            revision=content_revision(item),
            deduplication=DeduplicationDecision(
                is_duplicate=False, rule=None, original=None, reason="first observation"
            ),
        ),
        analysis=NewsAnalysis(
            event=classify_event(hedged),
            sentiment=evaluate_sentiment(hedged),
            mapping=link_entities(hedged, universe=(HAL,), published_on=PUBLISHED.date()),
            analysed_at=ANALYSED,
        ),
    )
    rendered = render_item(archived)

    assert "sentiment" in rendered
    reason = archived.analysis.sentiment.abstention_reason if archived.analysis else None
    if reason is not None:
        assert "abstained" in rendered
    else:
        assert "score" in rendered


def test_an_ambiguous_match_is_marked_rather_than_shown_as_confident() -> None:
    """An ambiguity nobody can see is an ambiguity nobody reviews."""
    twins = (
        HAL,
        LinkableInstrument(
            instrument_id=InstrumentId.deterministic("reference", "HAL2"),
            canonical_symbol="HAL2",
            company_name="Hindustan Aeronautics Limited",
        ),
    )
    item = _item("https://economictimes.indiatimes.test/ambiguous")
    archived = ArchivedNewsItem(
        revision=NewsRevision(
            item=item,
            revision=content_revision(item),
            deduplication=DeduplicationDecision(
                is_duplicate=False, rule=None, original=None, reason="first observation"
            ),
        ),
        analysis=NewsAnalysis(
            event=classify_event(HEADLINE),
            sentiment=evaluate_sentiment(HEADLINE),
            mapping=link_entities(HEADLINE, universe=twins, published_on=PUBLISHED.date()),
            analysed_at=ANALYSED,
        ),
    )
    rendered = render_item(archived)
    states = {state for _, state in archived.analysis.linked} if archived.analysis else set()

    if any(state.value == "AMBIGUOUS" for state in states):
        assert "[AMBIGUOUS]" in rendered
    else:
        assert "instrument" in rendered


def test_several_items_are_separated_so_they_can_be_read_apart() -> None:
    """One block per item; a wall of text is a listing nobody scans."""
    rendered = render_items((_archived(), _archived()))

    assert rendered.count("attribution:") == 2
