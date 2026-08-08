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
    GetLatestArchivedInstrumentDiscovery,
    GetSharedWatchlist,
)
from dhruva.contexts.reference.domain import (
    ArchivedInstrumentDiscovery,
    FuturesAvailabilityStatus,
    FuturesContract,
    FuturesContractObservation,
    FuturesContractStatus,
    InstrumentDiscovery,
    InstrumentKind,
    WatchlistInstrument,
)

__all__ = [
    "ArchiveOwnerInstrumentMaster",
    "ArchivedInstrumentDiscovery",
    "ConfigureReferenceUniverse",
    "ConfigureReferenceUniverseCommand",
    "ConfigureReferenceUniverseResult",
    "DiscoverOwnerInstruments",
    "FuturesAvailabilityStatus",
    "FuturesContract",
    "FuturesContractObservation",
    "FuturesContractStatus",
    "GetArchivedInstrumentDiscovery",
    "GetLatestArchivedInstrumentDiscovery",
    "GetSharedWatchlist",
    "InstrumentDiscovery",
    "InstrumentKind",
    "WatchlistInstrument",
]
