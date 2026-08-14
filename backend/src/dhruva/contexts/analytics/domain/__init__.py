"""Domain layer.

Entities, value objects, domain services, domain events and PORTS.

This layer performs no I/O and imports no framework. It may import only
``dhruva.shared`` and the standard library. Enforced by ADR-001 layering
and the import-linter contract ``Clean Architecture layers``.
"""

from dhruva.contexts.analytics.domain.technical import (
    TECHNICAL_FEATURE_REVISION,
    AdjustmentEvidence,
    BenchmarkBasis,
    BenchmarkRegime,
    FeatureName,
    FeatureStatus,
    TechnicalBar,
    TechnicalFeature,
    TechnicalFeatureSet,
    TechnicalSeries,
    compute_technical_features,
    unavailable_technical_features,
)

__all__ = [
    "TECHNICAL_FEATURE_REVISION",
    "AdjustmentEvidence",
    "BenchmarkBasis",
    "BenchmarkRegime",
    "FeatureName",
    "FeatureStatus",
    "TechnicalBar",
    "TechnicalFeature",
    "TechnicalFeatureSet",
    "TechnicalSeries",
    "compute_technical_features",
    "unavailable_technical_features",
]
