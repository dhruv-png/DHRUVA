"""Transaction-local PostgreSQL tenant context (ADR-053, ADR-074)."""

from __future__ import annotations

from typing import Final

__all__ = ["SESSION_ACCOUNT_SETTING"]

#: Existing migration 0007 established this name. It is a PostgreSQL custom
#: setting rather than process-global state, and the Unit of Work always sets it
#: with ``is_local = true`` so a pooled connection cannot retain one tenant into
#: a later transaction.
SESSION_ACCOUNT_SETTING: Final = "dhruva.current_account_id"
