"""Reconstruct reference-domain revisions from primitive persistence records."""

from __future__ import annotations

from uuid import UUID, uuid5

from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    InstrumentKind,
    WatchlistMembershipRevision,
)
from dhruva.contexts.reference.infrastructure.persistence.records import (
    IdentityRevisionRecord,
    MembershipRevisionRecord,
)
from dhruva.shared.identity import AccountId, InstrumentId

__all__ = ["IdentityRevisionFactory", "MembershipRevisionFactory"]

_REFERENCE_REVISION_NAMESPACE = UUID("4384104b-fbb2-4dce-87fe-2109177d924d")


def _revision_id(kind: str, *parts: str) -> UUID:
    """Derive an idempotent persistence identity from a source revision key."""
    return uuid5(_REFERENCE_REVISION_NAMESPACE, "\x1f".join((kind, *parts)))


class IdentityRevisionFactory:
    """Map identity revisions without leaking SQLAlchemy into the domain."""

    __slots__ = ()

    def deconstruct(self, revision: InstrumentIdentityRevision) -> IdentityRevisionRecord:
        """Convert a domain identity revision to primitives."""
        return IdentityRevisionRecord(
            id=_revision_id(
                "identity",
                str(revision.instrument_id.value),
                revision.source,
                revision.source_revision,
            ),
            instrument_id=revision.instrument_id.value,
            kind=revision.kind.value,
            canonical_symbol=revision.canonical_symbol,
            company_name=revision.company_name,
            aliases=revision.aliases,
            former_names=revision.former_names,
            isin=revision.isin,
            sector=revision.sector,
            concentration_groups=revision.concentration_groups,
            cash_exchange=revision.cash_mapping.exchange,
            cash_trading_symbol=revision.cash_mapping.trading_symbol,
            provider=revision.cash_mapping.provider,
            instrument_token=revision.cash_mapping.instrument_token,
            exchange_token=revision.cash_mapping.exchange_token,
            futures_research_requested=revision.futures_research_requested,
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
            recorded_at=revision.recorded_at,
            source=revision.source,
            source_revision=revision.source_revision,
        )

    def reconstruct(self, record: IdentityRevisionRecord) -> InstrumentIdentityRevision:
        """Reassert every domain invariant while loading an identity revision."""
        return InstrumentIdentityRevision(
            instrument_id=InstrumentId(record.instrument_id),
            kind=InstrumentKind(record.kind),
            canonical_symbol=record.canonical_symbol,
            company_name=record.company_name,
            aliases=record.aliases,
            former_names=record.former_names,
            isin=record.isin,
            sector=record.sector,
            concentration_groups=record.concentration_groups,
            cash_mapping=CashInstrumentMapping(
                exchange=record.cash_exchange,
                trading_symbol=record.cash_trading_symbol,
                provider=record.provider,
                instrument_token=record.instrument_token,
                exchange_token=record.exchange_token,
            ),
            futures_research_requested=record.futures_research_requested,
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            recorded_at=record.recorded_at,
            source=record.source,
            source_revision=record.source_revision,
        )


class MembershipRevisionFactory:
    """Map membership revisions without leaking SQLAlchemy into the domain."""

    __slots__ = ()

    def deconstruct(self, revision: WatchlistMembershipRevision) -> MembershipRevisionRecord:
        """Convert a domain membership revision to primitives."""
        return MembershipRevisionRecord(
            id=_revision_id(
                "membership",
                str(revision.account_id.value),
                str(revision.instrument_id.value),
                revision.source,
                revision.source_revision,
            ),
            account_id=revision.account_id.value,
            instrument_id=revision.instrument_id.value,
            active_from=revision.active_from,
            active_to=revision.active_to,
            recorded_at=revision.recorded_at,
            source=revision.source,
            source_revision=revision.source_revision,
        )

    def reconstruct(self, record: MembershipRevisionRecord) -> WatchlistMembershipRevision:
        """Reassert every domain invariant while loading membership."""
        return WatchlistMembershipRevision(
            account_id=AccountId(record.account_id),
            instrument_id=InstrumentId(record.instrument_id),
            active_from=record.active_from,
            active_to=record.active_to,
            recorded_at=record.recorded_at,
            source=record.source,
            source_revision=record.source_revision,
        )
