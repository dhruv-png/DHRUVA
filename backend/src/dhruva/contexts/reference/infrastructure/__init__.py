"""Infrastructure layer.

ADAPTERS: SQLAlchemy repositories, broker clients, message-bus adapters,
HTTP clients. Implements the ports declared in ``domain``.

May import this context's ``domain`` and ``application``. Nothing outside a
composition root may import this package (ADR-003).
"""

from dhruva.contexts.reference.infrastructure.owner_universe import (
    OWNER_UNIVERSE_REVISION,
    load_owner_universe,
)
from dhruva.contexts.reference.infrastructure.persistence import (
    InstrumentArchiveRepository,
    ReferenceRepository,
    SqlAlchemyReferenceUnitOfWork,
)
from dhruva.contexts.reference.infrastructure.zerodha_instruments import (
    KiteInstrumentMasterAdapter,
    parse_instrument_master,
)

__all__ = [
    "OWNER_UNIVERSE_REVISION",
    "InstrumentArchiveRepository",
    "KiteInstrumentMasterAdapter",
    "ReferenceRepository",
    "SqlAlchemyReferenceUnitOfWork",
    "load_owner_universe",
    "parse_instrument_master",
]
