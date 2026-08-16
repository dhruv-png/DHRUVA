"""Domain layer.

Entities, value objects, domain services, domain events and PORTS.

This layer performs no I/O and imports no framework. It may import only
``dhruva.shared`` and the standard library. Enforced by ADR-001 layering
and the import-linter contract ``Clean Architecture layers``.
"""

from dhruva.contexts.analytics.domain.evaluation import (
    CANDIDATE_OUTCOME_REVISION,
    DEFAULT_EVALUATION_HORIZONS,
    FutureOutcome,
    OutcomeStatus,
    calculate_future_outcome,
)
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
    "CANDIDATE_OUTCOME_REVISION",
    "DEFAULT_EVALUATION_HORIZONS",
    "TECHNICAL_FEATURE_REVISION",
    "AdjustmentEvidence",
    "BenchmarkBasis",
    "BenchmarkRegime",
    "FeatureName",
    "FeatureStatus",
    "FutureOutcome",
    "OutcomeStatus",
    "TechnicalBar",
    "TechnicalFeature",
    "TechnicalFeatureSet",
    "TechnicalSeries",
    "calculate_future_outcome",
    "compute_technical_features",
    "unavailable_technical_features",
]
