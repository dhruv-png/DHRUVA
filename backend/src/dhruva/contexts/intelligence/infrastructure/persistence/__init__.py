"""PostgreSQL persistence for the point-in-time news archive."""

from dhruva.contexts.intelligence.infrastructure.persistence.repository import (
    NewsRepository,
    ResearchObservationRepository,
)
from dhruva.contexts.intelligence.infrastructure.persistence.unit_of_work import (
    SqlAlchemyIntelligenceUnitOfWork,
)

__all__ = [
    "NewsRepository",
    "ResearchObservationRepository",
    "SqlAlchemyIntelligenceUnitOfWork",
]
