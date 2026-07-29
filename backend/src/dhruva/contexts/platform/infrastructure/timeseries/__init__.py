"""The ORM-bypass write path (ADR-054).

This package exists so that boundary rule R8 has something to enforce: no module
under a ``timeseries`` infrastructure package may import from any ``domain``
package. The rule is what keeps the exception an exception -- a business entity
cannot travel this way, because a business entity cannot be named here.

Everything on this path deals in column rows. See
:class:`dhruva.shared.persistence.TimeSeriesStorage` for the seven conditions
under which data qualifies.
"""

from __future__ import annotations

from dhruva.contexts.platform.infrastructure.timeseries.storage import (
    PostgresTimeSeriesStorage,
    UnknownTimeSeriesTableError,
)

__all__ = ["PostgresTimeSeriesStorage", "UnknownTimeSeriesTableError"]
