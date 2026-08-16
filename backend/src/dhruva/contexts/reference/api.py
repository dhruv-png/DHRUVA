"""Public API of the C1 ``reference`` context.

This module is the **only** import surface other contexts may use. Importing
``dhruva.contexts.reference.domain``, ``.application``, ``.infrastructure`` or
``.interfaces`` from another context is a build failure.

Re-export here only the DTOs and application services other contexts are
permitted to depend on. Provider and persistence adapters remain internal.
"""

from __future__ import annotations

from dhruva.contexts.reference.application import (
    ArchiveOwnerInstrumentMaster,
    ConfigureReferenceUniverse,
    ConfigureReferenceUniverseCommand,
    ConfigureReferenceUniverseResult,
    DiscoverOwnerInstruments,
    GetArchivedInstrumentDiscovery,
    GetCorporateActions,
    GetHistoricalUniverse,
    GetLatestArchivedInstrumentDiscovery,
    GetSharedWatchlist,
    RegisterCorporateActions,
    RegisterCorporateActionsResult,
    RegisterHistoricalUniverse,
    RegisterHistoricalUniverseCommand,
    RegisterHistoricalUniverseResult,
)
from dhruva.contexts.reference.domain import (
    ArchivedInstrumentDiscovery,
    CorporateActionEvidence,
    CorporateActionType,
    EvidenceVerification,
    FuturesAvailabilityStatus,
    FuturesContract,
    FuturesContractObservation,
    FuturesContractStatus,
    HistoricalDataQualityReason,
    HistoricalUniverseDefinition,
    HistoricalUniverseMember,
    HistoricalUniverseMembershipRevision,
    InstrumentDiscovery,
    InstrumentKind,
    MembershipReason,
    ResolvedHistoricalUniverse,
    ReturnBasis,
    SourceDiligenceStatus,
    SurvivorshipAssessment,
    SurvivorshipStatus,
    UniverseKind,
    WatchlistInstrument,
)

__all__ = [
    "ArchiveOwnerInstrumentMaster",
    "ArchivedInstrumentDiscovery",
    "ConfigureReferenceUniverse",
    "ConfigureReferenceUniverseCommand",
    "ConfigureReferenceUniverseResult",
    "CorporateActionEvidence",
    "CorporateActionType",
    "DiscoverOwnerInstruments",
    "EvidenceVerification",
    "FuturesAvailabilityStatus",
    "FuturesContract",
    "FuturesContractObservation",
    "FuturesContractStatus",
    "GetArchivedInstrumentDiscovery",
    "GetCorporateActions",
    "GetHistoricalUniverse",
    "GetLatestArchivedInstrumentDiscovery",
    "GetSharedWatchlist",
    "HistoricalDataQualityReason",
    "HistoricalUniverseDefinition",
    "HistoricalUniverseMember",
    "HistoricalUniverseMembershipRevision",
    "InstrumentDiscovery",
    "InstrumentKind",
    "MembershipReason",
    "RegisterCorporateActions",
    "RegisterCorporateActionsResult",
    "RegisterHistoricalUniverse",
    "RegisterHistoricalUniverseCommand",
    "RegisterHistoricalUniverseResult",
    "ResolvedHistoricalUniverse",
    "ReturnBasis",
    "SourceDiligenceStatus",
    "SurvivorshipAssessment",
    "SurvivorshipStatus",
    "UniverseKind",
    "WatchlistInstrument",
]
