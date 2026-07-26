"""D.H.R.U.V.A -- Dynamic Heuristic Regime Understanding & Volatility Analytics.

An institutional-grade quantitative analytics platform for the Indian equity and
derivatives markets.

Architecture
------------
Modular monolith with build-time-enforced bounded contexts (ADR-001). Four
processes -- ``api``, ``ingest``, ``worker``, ``scheduler`` -- share one codebase
and one dependency graph.

Governing document
------------------
``docs/DHRUVA_MASTER_PROJECT_PLAN.md``. Decisions are
recorded in ``docs/adr/`` and are immutable once accepted (ADR-027).

Delivery stage
--------------
Stage 1 -- Minimum Credible Product. Analytics only; no execution capability
exists in this repository (ADR-028).
"""

from __future__ import annotations

from dhruva.__about__ import __version__

__all__ = ["__version__"]
