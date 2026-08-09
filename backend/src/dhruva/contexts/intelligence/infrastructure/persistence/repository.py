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

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsArchiveWrite,
    NewsRevision,
)
from dhruva.contexts.intelligence.domain.attention import AttentionBand
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
from dhruva.contexts.intelligence.domain.research_observation import (
    AttentionObservationMember,
    ObservationAppendResult,
    ObservationProvenance,
    ObservationSourceHealth,
    ObservationSourceStatus,
    ObservationType,
    ResearchObservation,
    StoredResearchObservation,
)
from dhruva.contexts.intelligence.domain.sentiment import (
    AbstentionReason,
    SentimentLabel,
    SentimentResult,
)
from dhruva.contexts.intelligence.infrastructure.persistence.models import (
    AttentionObservationMemberModel,
    NewsAnalysisModel,
    NewsEntityLinkModel,
    NewsItemRevisionModel,
    ResearchObservationModel,
)
from dhruva.shared.errors import ConflictError, ValidationError
from dhruva.shared.identity import AccountId, InstrumentId

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["NewsRepository", "ResearchObservationRepository"]

_NEWS_NAMESPACE = UUID("b0f1c2d3-4e5a-4b6c-8d7e-9f0a1b2c3d4e")
_RESEARCH_NAMESPACE = UUID("8c5b3d2a-09ec-47f7-a59b-945987f81a36")


def _observation_id(observation: ResearchObservation) -> UUID:
    """Derive stable row identity from the logical observation identity."""
    return uuid5(
        _RESEARCH_NAMESPACE,
        "\x1f".join(
            (
                str(observation.account_id),
                observation.observation_type.value,
                observation.cutoff.isoformat(),
                observation.observation_sha256,
            )
        ),
    )


class ResearchObservationRepository:
    """Append immutable research facts and read only the bound account's history."""

    __slots__ = ("_account_id", "_session")

    def __init__(self, session: AsyncSession, *, account_id: AccountId) -> None:
        """Bind persistence to one transaction and one tenant identity."""
        self._session = session
        self._account_id = account_id

    async def append(self, observation: ResearchObservation) -> ObservationAppendResult:
        """Append a new fingerprint or return the identical fact already stored."""
        if observation.account_id != self._account_id:
            raise ValidationError("research observation account does not match transaction")

        # There may be no row to SELECT FOR UPDATE on the first write.  A
        # transaction advisory lock serialises this one account/cutoff/type
        # stream so two concurrent corrections cannot fork the supersession
        # chain.  It releases automatically with the UoW transaction.
        stream = "\x1f".join(
            (
                str(self._account_id),
                observation.observation_type.value,
                observation.cutoff.isoformat(),
            )
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:stream, 0))"),
            {"stream": stream},
        )

        existing = await self._existing(observation)
        if existing is not None:
            return ObservationAppendResult(stored=await self._stored(existing), created=False)

        latest = await self._latest_in_stream(observation)
        if latest is not None and observation.recorded_at < latest.recorded_at:
            raise ConflictError(
                "a correcting observation cannot be recorded before the fact it supersedes",
                cutoff=observation.cutoff.isoformat(),
            )

        row_id = _observation_id(observation)
        model = ResearchObservationModel(
            **_observation_values(
                row_id,
                observation,
                supersedes_id=None if latest is None else latest.id,
            )
        )
        self._session.add(model)
        # There is no ORM relationship on these persistence-only models.
        # Flush the parent explicitly so SQLAlchemy cannot batch the member
        # INSERTs ahead of the composite foreign-key target.
        await self._session.flush()
        self._session.add_all(
            AttentionObservationMemberModel(
                **_member_values(row_id, observation.account_id, member)
            )
            for member in observation.members
        )
        await self._session.flush()
        return ObservationAppendResult(
            stored=StoredResearchObservation(
                observation=observation,
                supersedes_sha256=None if latest is None else latest.observation_sha256,
            ),
            created=True,
        )

    async def list_recent(self, *, limit: int) -> tuple[StoredResearchObservation, ...]:
        """Return recent observations for this account and no other."""
        rows = (
            (
                await self._session.execute(
                    select(ResearchObservationModel)
                    .where(ResearchObservationModel.account_id == self._account_id.value)
                    .order_by(
                        ResearchObservationModel.cutoff.desc(),
                        ResearchObservationModel.recorded_at.desc(),
                        ResearchObservationModel.id.desc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple([await self._stored(row) for row in rows])

    async def _existing(self, observation: ResearchObservation) -> ResearchObservationModel | None:
        """Find the one row with this complete logical identity."""
        return (
            (
                await self._session.execute(
                    select(ResearchObservationModel).where(
                        ResearchObservationModel.account_id == self._account_id.value,
                        ResearchObservationModel.observation_type
                        == observation.observation_type.value,
                        ResearchObservationModel.cutoff == observation.cutoff,
                        ResearchObservationModel.observation_sha256
                        == observation.observation_sha256,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    async def _latest_in_stream(
        self, observation: ResearchObservation
    ) -> ResearchObservationModel | None:
        """Return the fact a genuinely changed same-cutoff observation supersedes."""
        return (
            (
                await self._session.execute(
                    select(ResearchObservationModel)
                    .where(
                        ResearchObservationModel.account_id == self._account_id.value,
                        ResearchObservationModel.observation_type
                        == observation.observation_type.value,
                        ResearchObservationModel.cutoff == observation.cutoff,
                    )
                    .order_by(
                        ResearchObservationModel.recorded_at.desc(),
                        ResearchObservationModel.id.desc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .one_or_none()
        )

    async def _stored(self, model: ResearchObservationModel) -> StoredResearchObservation:
        """Reconstruct and revalidate a complete observation from primitive rows."""
        members = (
            (
                await self._session.execute(
                    select(AttentionObservationMemberModel)
                    .where(
                        AttentionObservationMemberModel.observation_id == model.id,
                        AttentionObservationMemberModel.account_id == self._account_id.value,
                    )
                    .order_by(AttentionObservationMemberModel.rank)
                )
            )
            .scalars()
            .all()
        )
        observation = _domain_observation(model, tuple(members))
        if model.ranked_count != len(observation.members):
            raise ConflictError("research observation member count does not match its header")
        if model.attention_count != observation.attention_count:
            raise ConflictError("research observation attention count does not match its members")
        supersedes = None
        if model.supersedes_id is not None:
            supersedes = await self._session.scalar(
                select(ResearchObservationModel.observation_sha256).where(
                    ResearchObservationModel.id == model.supersedes_id,
                    ResearchObservationModel.account_id == self._account_id.value,
                )
            )
            if supersedes is None:
                raise ConflictError("research observation supersession target is missing")
        return StoredResearchObservation(observation=observation, supersedes_sha256=supersedes)


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
                    content_hash=model.content_revision,
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


def _observation_values(
    row_id: UUID,
    observation: ResearchObservation,
    *,
    supersedes_id: UUID | None,
) -> dict[str, object]:
    """Decompose one validated observation header into primitive columns."""
    provenance = observation.provenance
    health = observation.source_health
    return {
        "id": row_id,
        "account_id": observation.account_id.value,
        "observation_type": observation.observation_type.value,
        "cutoff": observation.cutoff,
        "recorded_at": observation.recorded_at,
        "run_status": observation.status.value,
        "market_source_status": health.market.value,
        "news_source_status": health.news.value,
        "degraded_reasons": list(health.degraded_reasons),
        "attention_revision": provenance.attention_revision,
        "digest_revision": provenance.digest_revision,
        "digest_policy_revision": provenance.digest_policy_revision,
        "entity_linking_revision": provenance.entity_linking_revision,
        "event_classification_revision": provenance.event_classification_revision,
        "market_context_revision": provenance.market_context_revision,
        "news_identity_revision": provenance.news_identity_revision,
        "sentiment_revision": provenance.sentiment_revision,
        "packet_schema_revision": provenance.packet_schema_revision,
        "observation_schema_revision": provenance.observation_schema_revision,
        "universe_sha256": observation.universe_sha256,
        "observation_sha256": observation.observation_sha256,
        "packet_body_sha256": observation.packet_body_sha256,
        "ranked_count": len(observation.members),
        "attention_count": observation.attention_count,
        "supersedes_id": supersedes_id,
    }


def _member_values(
    observation_id: UUID,
    account_id: AccountId,
    member: AttentionObservationMember,
) -> dict[str, object]:
    """Decompose one validated ranked member into primitive columns."""
    return {
        "observation_id": observation_id,
        "instrument_id": member.instrument_id.value,
        "account_id": account_id.value,
        "rank": member.rank,
        "canonical_symbol": member.canonical_symbol,
        "company_name": member.company_name,
        "score": member.score,
        "band": member.band.value,
        "reasons": list(member.reasons),
        "market_context_available": member.market_context_available,
        "market_context_sha256": member.market_context_sha256,
        "archived_news_revisions": list(member.archived_news_revisions),
        "news_items_withheld": member.news_items_withheld,
    }


def _domain_observation(
    model: ResearchObservationModel,
    members: tuple[AttentionObservationMemberModel, ...],
) -> ResearchObservation:
    """Rebuild the domain fact so reads re-run every invariant and fingerprint."""
    return ResearchObservation(
        account_id=AccountId(model.account_id),
        observation_type=ObservationType(model.observation_type),
        cutoff=model.cutoff,
        recorded_at=model.recorded_at,
        provenance=ObservationProvenance(
            attention_revision=model.attention_revision,
            digest_revision=model.digest_revision,
            digest_policy_revision=model.digest_policy_revision,
            entity_linking_revision=model.entity_linking_revision,
            event_classification_revision=model.event_classification_revision,
            market_context_revision=model.market_context_revision,
            news_identity_revision=model.news_identity_revision,
            sentiment_revision=model.sentiment_revision,
            packet_schema_revision=model.packet_schema_revision,
            observation_schema_revision=model.observation_schema_revision,
        ),
        source_health=ObservationSourceHealth(
            market=ObservationSourceStatus(model.market_source_status),
            news=ObservationSourceStatus(model.news_source_status),
            degraded_reasons=tuple(model.degraded_reasons),
        ),
        members=tuple(
            AttentionObservationMember(
                instrument_id=InstrumentId(member.instrument_id),
                rank=member.rank,
                canonical_symbol=member.canonical_symbol,
                company_name=member.company_name,
                score=member.score,
                band=AttentionBand(member.band),
                reasons=tuple(member.reasons),
                market_context_available=member.market_context_available,
                market_context_sha256=member.market_context_sha256,
                archived_news_revisions=tuple(member.archived_news_revisions),
                news_items_withheld=member.news_items_withheld,
            )
            for member in members
        ),
        universe_sha256=model.universe_sha256,
        observation_sha256=model.observation_sha256,
        packet_body_sha256=model.packet_body_sha256,
    )
