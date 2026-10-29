"""Storage backends: SQLite (default) and PostgreSQL, behind one `Store` interface.

`open_store(settings)` returns a connection for the backend the settings choose:
PostgreSQL when `KB_DATABASE_URL` is set, else the SQLite file `KB_DB`. Callers
close it when done (PostgreSQL: back to the pool).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kb.store.base import Dialect, Store

if TYPE_CHECKING:
    from kb.config import Settings

__all__ = ["Dialect", "Store", "close_pools", "init_schema", "open_store"]


def open_store(settings: "Settings") -> Store:
    """Open a connection to the configured database (does not create the schema on SQLite)."""
    if settings.database_url:
        from kb.store.postgres import open_postgres

        return open_postgres(settings.database_url)
    from kb import db

    return db.connect(settings.db_path)


def init_schema(store: Store) -> None:
    """Create the tables if missing (idempotent), for either backend."""
    from kb import db

    db.init_schema(store)  # type: ignore[arg-type]


def close_pools() -> None:
    """Close the PostgreSQL pools, if any were opened (application shutdown)."""
    import sys

    if "kb.store.postgres" in sys.modules:
        sys.modules["kb.store.postgres"].close_pools()
