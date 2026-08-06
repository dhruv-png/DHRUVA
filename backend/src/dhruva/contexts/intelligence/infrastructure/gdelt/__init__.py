"""Read-only GDELT DOC 2.0 adapter: transport, and the pure mapper beside it."""

from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_ATTRIBUTION_URL,
    GDELT_MAPPER_REVISION,
    GDELT_SOURCE_KEY,
    gdelt_source,
    map_artlist,
)

__all__ = [
    "GDELT_ATTRIBUTION_URL",
    "GDELT_MAPPER_REVISION",
    "GDELT_SOURCE_KEY",
    "gdelt_source",
    "map_artlist",
]
