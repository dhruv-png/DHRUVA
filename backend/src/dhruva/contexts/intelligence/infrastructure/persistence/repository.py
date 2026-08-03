"""Append-only news repository with point-in-time reads.

Every identifier is derived with ``uuid5`` from the natural key, so the same
observation computes the same row id in every process. That is what makes a
re-poll an ``ON CONFLICT DO NOTHING`` no-op instead of a duplicate, and it is
also why ``first_seen_at`` is deliberately *not* part of any key: the second
poll must leave the first observation's timestamp exactly where it was, or the
archive would quietly claim DHRUVA saw everything for the first time today.

There is no update path. A correction arrives as a new content revision beside
the old one, and the read picks the latest revision observable at the caller's
cutoff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsArchiveWrite,
    NewsRevision,
)
from dhruva.contexts.intelligence.domain.entity_linking import (
    EntityLinkResult,
    EntityMatch,
    MatchKind,
    MatchState,
)
from dhruva.contexts.intelligence.domain.events import EventCategory, EventClassification
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    DeduplicationRule,
    NewsFingerprints,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
)
from dhruva.contexts.intelligence.domain.sentiment import (
    AbstentionReason,
    SentimentLabel,
    SentimentResult,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    NewsAnalysisModel,
    NewsEntityLinkModel,
    NewsItemRevisionModel,
)
from dhruva.shared.errors import ConflictError, ValidationError
from dhruva.shared.identity import InstrumentId

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["NewsRepository"]

_NEWS_NAMESPACE = UUID("b0f1c2d3-4e5a-4b6c-8d7e-9f0a1b2c3d4e")


def _revision_id(revision: NewsRevision) -> UUID:
    """Derive the stable row identity of one observed revision."""
    return uuid5(
        _NEWS_NAMESPACE,
        "\x1f".join(
            (
                "revision",
                revision.item.identity.source_key,
                revision.item.identity.provider_item_id,
                revision.revision,
            )
        ),
    )


def _analysis_id(revision_id: UUID, analysis: NewsAnalysis) -> UUID:
    """Derive the stable row identity of one ruleset pass over one revision."""
    return uuid5(
        _NEWS_NAMESPACE,
        "\x1f".join(
            (
                "analysis",
                str(revision_id),
                analysis.event.revision,
                analysis.sentiment.ruleset_revision,
                analysis.mapping.revision,
            )
        ),
    )


def _link_id(analysis_id: UUID, instrument_id: InstrumentId, state: MatchState) -> UUID:
    """Derive the stable row identity of one linked instrument."""
    return uuid5(
        _NEWS_NAMESPACE,
        "\x1f".join(("link", str(analysis_id), str(instrument_id.value), state.value)),
    )


class NewsRepository:
    """Append revisions, analyses and links; resolve what was knowable when."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to one caller-owned transaction."""
        self._session = session

    async def append(
        self,
        revisions: tuple[tuple[NewsRevision, NewsAnalysis | None], ...],
    ) -> NewsArchiveWrite:
        """Stage every unseen revision and analysis, reporting identical retries."""
        if not revisions:
            return NewsArchiveWrite(
                revisions_added=0,
                revisions_unchanged=0,
                analyses_added=0,
                links_added=0,
            )

        rows = {_revision_id(revision): revision for revision, _ in revisions}
        if len(rows) != len(revisions):
            raise ValidationError("one news append cannot contain a revision twice")

        added = await self._append_revisions(rows)
        await self._verify_unchanged(rows, added=added)
        analyses_added, links_added = await self._append_analyses(revisions)
        return NewsArchiveWrite(
            revisions_added=len(added),
            revisions_unchanged=len(rows) - len(added),
            analyses_added=analyses_added,
            links_added=links_added,
        )

    async def _append_revisions(self, rows: dict[UUID, NewsRevision]) -> frozenset[UUID]:
        """Insert unseen revisions and return exactly those the database took."""
        result = await self._session.execute(
            insert(NewsItemRevisionModel)
            .values([_revision_values(key, value) for key, value in rows.items()])
            .on_conflict_do_nothing(index_elements=[NewsItemRevisionModel.id])
            .returning(NewsItemRevisionModel.id)
        )
        return frozenset(result.scalars().all())

    async def _verify_unchanged(
        self,
        rows: dict[UUID, NewsRevision],
        *,
        added: frozenset[UUID],
    ) -> None:
        """Refuse a stored row whose content differs from the one just observed.

        The content revision is part of the key, so this should be unreachable.
        It is checked anyway: an identifier that silently covers two different
        headlines is the one defect this archive could never explain later.
        """
        for key, revision in rows.items():
            if key in added:
                continue
            stored = await self._session.get(NewsItemRevisionModel, key)
            if stored is None:
                continue
            if (
                stored.canonical_url != revision.item.identity.url
                or stored.title != revision.item.text.title
                or stored.snippet != revision.item.text.snippet
                or stored.published_at != revision.item.published_at
            ):
                raise ConflictError(
                    "a news content revision was reused with different content",
                    source_key=revision.item.identity.source_key,
                    provider_item_id=revision.item.identity.provider_item_id,
                )

    async def _append_analyses(
        self,
        revisions: tuple[tuple[NewsRevision, NewsAnalysis | None], ...],
    ) -> tuple[int, int]:
        """Insert unseen analyses and their links, returning both counts."""
        analysis_values: list[dict[str, object]] = []
        link_values: list[dict[str, object]] = []
        for revision, analysis in revisions:
            if analysis is None:
                continue
            revision_id = _revision_id(revision)
            analysis_id = _analysis_id(revision_id, analysis)
            analysis_values.append(_analysis_values(analysis_id, revision_id, analysis))
            link_values.extend(
                _link_values(analysis_id, match, state) for match, state in analysis.linked
            )
        if not analysis_values:
            return 0, 0

        analyses = await self._session.execute(
            insert(NewsAnalysisModel)
            .values(analysis_values)
            .on_conflict_do_nothing(index_elements=[NewsAnalysisModel.id])
            .returning(NewsAnalysisModel.id)
        )
        analyses_added = len(analyses.scalars().all())
        if not link_values:
            return analyses_added, 0
        links = await self._session.execute(
            insert(NewsEntityLinkModel)
            .values(link_values)
            .on_conflict_do_nothing(index_elements=[NewsEntityLinkModel.id])
            .returning(NewsEntityLinkModel.id)
        )
        return analyses_added, len(links.scalars().all())

    async def list_known_at(
        self,
        *,
        known_at: datetime,
        published_from: datetime,
        published_to: datetime,
        canonical_symbol: str | None = None,
    ) -> tuple[ArchivedNewsItem, ...]:
        """Return the latest revision of each item observable at ``known_at``."""
        _validate_window(known_at, published_from, published_to)
        models = (
            (
                await self._session.execute(
                    select(NewsItemRevisionModel).where(
                        NewsItemRevisionModel.first_seen_at <= known_at,
                        NewsItemRevisionModel.published_at >= published_from,
                        NewsItemRevisionModel.published_at <= published_to,
                    )
                )
            )
            .scalars()
            .all()
        )
        latest: dict[tuple[str, str], NewsItemRevisionModel] = {}
        for model in models:
            key = (model.source_key, model.provider_item_id)
            prior = latest.get(key)
            if prior is None or (model.first_seen_at, model.id.int) > (
                prior.first_seen_at,
                prior.id.int,
            ):
                latest[key] = model

        archived: list[ArchivedNewsItem] = []
        for model in sorted(latest.values(), key=lambda item: (item.published_at, item.id.int)):
            analysis = await self._latest_analysis(model.id, known_at=known_at)
            if canonical_symbol is not None and not _mentions(analysis, canonical_symbol):
                continue
            archived.append(ArchivedNewsItem(revision=_domain_revision(model), analysis=analysis))
        return tuple(archived)

    async def _latest_analysis(
        self,
        revision_id: UUID,
        *,
        known_at: datetime,
    ) -> NewsAnalysis | None:
        """Return the most recent analysis of one revision observable at the cutoff."""
        model = (
            (
                await self._session.execute(
                    select(NewsAnalysisModel)
                    .where(
                        NewsAnalysisModel.news_item_revision_id == revision_id,
                        NewsAnalysisModel.analysed_at <= known_at,
                    )
                    .order_by(
                        NewsAnalysisModel.analysed_at.desc(),
                        NewsAnalysisModel.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if model is None:
            return None
        links = (
            (
                await self._session.execute(
                    select(NewsEntityLinkModel)
                    .where(NewsEntityLinkModel.news_analysis_id == model.id)
                    .order_by(NewsEntityLinkModel.canonical_symbol)
                )
            )
            .scalars()
            .all()
        )
        return _domain_analysis(model, tuple(links))

    async def fingerprints_seen_since(
        self,
        *,
        published_from: datetime,
        known_at: datetime,
    ) -> tuple[tuple[NewsItemIdentity, NewsFingerprints], ...]:
        """Return stored fingerprints a new poll can be deduplicated against."""
        if published_from > known_at:
            raise ValidationError("fingerprint window starts after the knowledge cutoff")
        models = (
            (
                await self._session.execute(
                    select(NewsItemRevisionModel)
                    .where(
                        NewsItemRevisionModel.first_seen_at <= known_at,
                        NewsItemRevisionModel.published_at >= published_from,
                        NewsItemRevisionModel.duplicate_rule.is_(None),
                    )
                    .order_by(
                        NewsItemRevisionModel.first_seen_at,
                        NewsItemRevisionModel.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            (
                NewsItemIdentity(
                    source_key=model.source_key,
                    provider_item_id=model.provider_item_id,
                    url=model.canonical_url,
                ),
                NewsFingerprints(
                    identity=model.fingerprint_identity,
                    url=model.fingerprint_url,
                    headline=model.fingerprint_headline,
                    rewrite=model.fingerprint_rewrite,
                    revision=model.identity_revision,
                ),
            )
            for model in models
        )


def _validate_window(known_at: datetime, published_from: datetime, published_to: datetime) -> None:
    """Reject a query that cannot describe a point in time."""
    for name, value in (
        ("known_at", known_at),
        ("published_from", published_from),
        ("published_to", published_to),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(f"{name} must be timezone-aware")
    if published_from > published_to:
        raise ValidationError("news publication window is reversed")


def _mentions(analysis: NewsAnalysis | None, canonical_symbol: str) -> bool:
    """Return whether an analysis confidently linked one canonical symbol."""
    return analysis is not None and any(
        match.canonical_symbol == canonical_symbol for match in analysis.mapping.matches
    )


def _revision_values(key: UUID, revision: NewsRevision) -> dict[str, object]:
    """Decompose one validated revision into primitive columns."""
    item = revision.item
    prints = item.fingerprints
    decision = revision.deduplication
    return {
        "id": key,
        "source_key": item.source.key,
        "source_display_name": item.source.display_name,
        "source_tier": item.source.tier.value,
        "source_homepage_url": item.source.homepage_url,
        "provider_item_id": item.identity.provider_item_id,
        "canonical_url": item.identity.url,
        "title": item.text.title,
        "snippet": item.text.snippet,
        "published_at": item.published_at,
        "first_seen_at": item.first_seen_at,
        "content_revision": revision.revision,
        "fingerprint_identity": prints.identity,
        "fingerprint_url": prints.url,
        "fingerprint_headline": prints.headline,
        "fingerprint_rewrite": prints.rewrite,
        "duplicate_rule": None if decision.rule is None else decision.rule.value,
        "duplicate_of_source_key": None
        if decision.original is None
        else decision.original.source_key,
        "duplicate_of_provider_item_id": None
        if decision.original is None
        else decision.original.provider_item_id,
        "duplicate_of_url": None if decision.original is None else decision.original.url,
        "duplicate_reason": decision.reason,
        "identity_revision": prints.revision,
    }


def _analysis_values(key: UUID, revision_id: UUID, analysis: NewsAnalysis) -> dict[str, object]:
    """Decompose one ruleset pass into primitive columns."""
    return {
        "id": key,
        "news_item_revision_id": revision_id,
        "analysed_at": analysis.analysed_at,
        "event_category": analysis.event.category.value,
        "event_matched_phrase": analysis.event.matched_phrase,
        "event_reason": analysis.event.reason,
        "event_revision": analysis.event.revision,
        "sentiment_label": analysis.sentiment.label.value,
        "sentiment_score": analysis.sentiment.score,
        "sentiment_confidence": analysis.sentiment.confidence,
        "abstention_reason": None
        if analysis.sentiment.abstention_reason is None
        else analysis.sentiment.abstention_reason.value,
        "contrast_present": analysis.sentiment.contrast_present,
        "uncertainty_present": analysis.sentiment.uncertainty_present,
        "positive_terms": list(analysis.sentiment.positive_terms),
        "negative_terms": list(analysis.sentiment.negative_terms),
        "negated_terms": list(analysis.sentiment.negated_terms),
        "sentiment_input_sha256": analysis.sentiment.input_sha256,
        "sentiment_ruleset_revision": analysis.sentiment.ruleset_revision,
        "mapping_state": analysis.mapping.state.value,
        "mapping_reason": analysis.mapping.reason,
        "linking_revision": analysis.mapping.revision,
    }


def _link_values(analysis_id: UUID, match: EntityMatch, state: MatchState) -> dict[str, object]:
    """Decompose one linked instrument into primitive columns."""
    return {
        "id": _link_id(analysis_id, match.instrument_id, state),
        "news_analysis_id": analysis_id,
        "instrument_id": match.instrument_id.value,
        "canonical_symbol": match.canonical_symbol,
        "match_state": state.value,
        "match_kind": match.kind.value,
        "matched_text": match.matched_text,
        "relevance": match.relevance,
        "reason": match.reason,
    }


def _domain_revision(model: NewsItemRevisionModel) -> NewsRevision:
    """Reconstruct and revalidate one persisted revision."""
    item = NewsItem(
        identity=NewsItemIdentity(
            source_key=model.source_key,
            provider_item_id=model.provider_item_id,
            url=model.canonical_url,
        ),
        source=NewsSource(
            key=model.source_key,
            display_name=model.source_display_name,
            tier=NewsSourceTier(model.source_tier),
            homepage_url=model.source_homepage_url,
        ),
        text=PermittedText(title=model.title, snippet=model.snippet),
        published_at=model.published_at,
        first_seen_at=model.first_seen_at,
    )
    return NewsRevision(
        item=item,
        revision=model.content_revision,
        deduplication=DeduplicationDecision(
            is_duplicate=model.duplicate_rule is not None,
            rule=None if model.duplicate_rule is None else DeduplicationRule(model.duplicate_rule),
            original=_original_identity(model),
            reason=model.duplicate_reason,
            revision=model.identity_revision,
        ),
    )


def _original_identity(model: NewsItemRevisionModel) -> NewsItemIdentity | None:
    """Rebuild the identity this revision was found to repeat, if it repeated one."""
    if (
        model.duplicate_of_source_key is None
        or model.duplicate_of_provider_item_id is None
        or model.duplicate_of_url is None
    ):
        return None
    return NewsItemIdentity(
        source_key=model.duplicate_of_source_key,
        provider_item_id=model.duplicate_of_provider_item_id,
        url=model.duplicate_of_url,
    )


def _domain_analysis(
    model: NewsAnalysisModel,
    links: tuple[NewsEntityLinkModel, ...],
) -> NewsAnalysis:
    """Reconstruct and revalidate one persisted ruleset pass."""
    matches = tuple(_domain_match(link) for link in links if link.match_state == "MATCHED")
    ambiguous = tuple(_domain_match(link) for link in links if link.match_state == "AMBIGUOUS")
    return NewsAnalysis(
        event=EventClassification(
            category=EventCategory(model.event_category),
            matched_phrase=model.event_matched_phrase,
            reason=model.event_reason,
            revision=model.event_revision,
        ),
        sentiment=SentimentResult(
            label=SentimentLabel(model.sentiment_label),
            score=model.sentiment_score,
            confidence=model.sentiment_confidence,
            positive_terms=tuple(model.positive_terms),
            negative_terms=tuple(model.negative_terms),
            negated_terms=tuple(model.negated_terms),
            contrast_present=model.contrast_present,
            uncertainty_present=model.uncertainty_present,
            abstention_reason=None
            if model.abstention_reason is None
            else AbstentionReason(model.abstention_reason),
            input_sha256=model.sentiment_input_sha256,
            ruleset_revision=model.sentiment_ruleset_revision,
        ),
        mapping=EntityLinkResult(
            state=MatchState(model.mapping_state),
            matches=matches,
            ambiguous=ambiguous,
            reason=model.mapping_reason,
            revision=model.linking_revision,
        ),
        analysed_at=model.analysed_at,
    )


def _domain_match(link: NewsEntityLinkModel) -> EntityMatch:
    """Reconstruct one stored entity match from its primitive columns."""
    return EntityMatch(
        instrument_id=InstrumentId(link.instrument_id),
        canonical_symbol=link.canonical_symbol,
        matched_text=link.matched_text,
        kind=MatchKind(link.match_kind),
        relevance=link.relevance,
        reason=link.reason,
    )
