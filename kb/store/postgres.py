"""PostgreSQL store: a psycopg 3 pool, `?` placeholders, rows like `sqlite3.Row`.

One `ConnectionPool` per database URL is shared by the whole process (min 1,
max 5 connections). `open_postgres(url)` checks one connection out; `close()`
puts it back. Connections are in autocommit mode so statements outside
`transaction()` commit at once, as they do on SQLite.

What makes the rest of `kb/` dialect-blind:

- `?` placeholders become `%s`, and `INSERT OR IGNORE` becomes `ON CONFLICT DO
  NOTHING` (`translate`).
- `chunks.embedding` is `vector(384)` but callers pass and receive the same
  float32 bytes as on SQLite: a `bytes` parameter is sent as a vector, and a
  vector column is loaded as bytes (`_VectorBytesLoader`).
- `TIMESTAMPTZ` columns load as the ISO 8601 UTC strings SQLite stores
  (`2026-10-31T12:00:00Z`), and string parameters are cast by the server.
- Rows support `row["name"]`, `row[0]` and `keys()` (`PgRow`).

`transaction()` takes a transaction-scoped advisory lock, so write
transactions are serialized across every process that uses the database, the
way `BEGIN IMMEDIATE` serializes them on SQLite. The read-check-write sequences
in `service.py` (duplicate check, `_check_unchanged`) depend on that.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
from contextlib import contextmanager
from datetime import timezone
from importlib import resources
from typing import Any, Iterable, Iterator, Sequence

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from pgvector.psycopg.vector import VectorLoader
from psycopg.pq import Format
from psycopg.types import TypeInfo
from psycopg.types.datetime import TimestamptzLoader
from psycopg_pool import ConnectionPool

from kb.store.base import tag_filter_sql

log = logging.getLogger(__name__)

POOL_MIN = 1
POOL_MAX = 5
CONNECT_TIMEOUT = 10
# Advisory lock keys (arbitrary constants, unique to this application).
LOCK_WRITE = 7_351_001
LOCK_SCHEMA = 7_351_002

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# --- SQL translation ------------------------------------------------------------

_INSERT_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_IS_NOT_PARAM = re.compile(r"\bIS\s+NOT\s+\?", re.IGNORECASE)


@functools.lru_cache(maxsize=512)
def translate(sql: str) -> str:
    """Rewrite the SQLite flavor used in `kb/` into PostgreSQL.

    `?` becomes `%s` (no statement in `kb/` has a `?` inside a string literal),
    a literal `%` is doubled, `INSERT OR IGNORE` becomes `INSERT ... ON CONFLICT
    DO NOTHING` and `x IS NOT ?` (a NULL-safe inequality) becomes `x IS DISTINCT
    FROM ?`.
    """
    out = sql
    if _INSERT_IGNORE.match(out):
        out = _INSERT_IGNORE.sub("INSERT INTO", out, count=1).rstrip().rstrip(";")
        out += " ON CONFLICT DO NOTHING"
    out = _IS_NOT_PARAM.sub("IS DISTINCT FROM ?", out)
    return out.replace("%", "%%").replace("?", "%s")


def adapt_params(params: Sequence[Any] | None) -> list[Any]:
    """Convert parameters for PostgreSQL.

    `bytes` is always an embedding (float32, L2-normalized) and is sent as a
    `vector`. PostgreSQL text cannot hold NUL, so NUL characters are dropped from
    strings (a PDF extraction can contain them; SQLite keeps them).
    """
    out: list[Any] = []
    for p in params or ():
        if isinstance(p, (bytes, bytearray, memoryview)):
            out.append(np.frombuffer(p, dtype=np.float32))
        elif isinstance(p, str) and "\x00" in p:
            out.append(p.replace("\x00", ""))
        else:
            out.append(p)
    return out


# --- rows and type loaders ------------------------------------------------------


class PgRow(tuple):
    """A result row that also answers `row["column"]` and `keys()`, like `sqlite3.Row`."""

    _index: dict[str, int]

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._index[key])
            except KeyError:
                raise IndexError("No item with that key") from None
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._index)


def row_factory(cursor: Any) -> Any:
    """psycopg row factory producing `PgRow`."""
    description = cursor.description
    index = {c.name: i for i, c in enumerate(description)} if description else {}

    def make(values: Sequence[Any]) -> PgRow:
        row = PgRow(values)
        row._index = index
        return row

    return make


class _IsoTimestamptzLoader(TimestamptzLoader):
    """`timestamptz` as `YYYY-MM-DDTHH:MM:SSZ` in UTC, the format SQLite stores."""

    def load(self, data: Any) -> str:  # type: ignore[override]
        return super().load(data).astimezone(timezone.utc).strftime(_TIME_FORMAT)


class _VectorBytesLoader(VectorLoader):
    """`vector` as the float32 bytes the SQLite BLOB holds."""

    format = Format.TEXT

    def load(self, data: Any) -> bytes:  # type: ignore[override]
        text = bytes(data).decode("utf8").strip("[]")
        if not text:
            return b""
        return np.array(text.split(","), dtype=np.float32).tobytes()


def _configure(conn: psycopg.Connection) -> None:
    """Pool callback for every new connection: UTC, vector adapters, type loaders."""
    conn.execute("SET TIME ZONE 'UTC'")
    register_vector(conn)
    info = TypeInfo.fetch(conn, "vector")
    if info is None:  # pragma: no cover - the schema step created it
        raise psycopg.ProgrammingError("vector type not found in the database")
    conn.adapters.register_loader(info.oid, _VectorBytesLoader)
    conn.adapters.register_loader("timestamptz", _IsoTimestamptzLoader)


# --- schema and pool ------------------------------------------------------------


def schema_sql() -> str:
    """The idempotent PostgreSQL schema (`kb/store/schema_pg.sql`)."""
    return resources.files("kb.store").joinpath("schema_pg.sql").read_text(encoding="utf8")


def apply_schema(conn: psycopg.Connection) -> None:
    """Create the extension, tables and indexes if missing.

    Runs in one transaction under an advisory lock, so two instances that boot
    together cannot trip over each other's `CREATE TABLE`.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_SCHEMA,))
        conn.execute(schema_sql())  # type: ignore[arg-type]  # multi-statement script, no parameters


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def get_pool(url: str) -> ConnectionPool:
    """The process-wide pool for `url`, created (and the schema applied) on first use."""
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            with psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as boot:
                apply_schema(boot)
            pool = ConnectionPool(
                url,
                min_size=POOL_MIN,
                max_size=POOL_MAX,
                kwargs={
                    "autocommit": True,
                    "row_factory": row_factory,
                    "connect_timeout": CONNECT_TIMEOUT,
                },
                configure=_configure,
                open=False,
            )
            pool.open(wait=True, timeout=30)
            _pools[url] = pool
            log.info("postgres pool opened", extra={"min": POOL_MIN, "max": POOL_MAX})
        return pool


def close_pools() -> None:
    """Close every pool (application shutdown, tests)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:  # shutting down; nothing useful to do
            log.warning("closing a postgres pool failed", exc_info=True)


def open_postgres(url: str) -> "PostgresStore":
    """Check a connection out of the pool for `url`."""
    return PostgresStore(get_pool(url))


# --- the store ------------------------------------------------------------------


class PostgresStore:
    """A pooled PostgreSQL connection with the `Store` interface (see `kb/store/base.py`)."""

    dialect = "postgres"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._conn: psycopg.Connection | None = pool.getconn()

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise psycopg.InterfaceError("the store is closed")
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.conn.execute(translate(sql), adapt_params(params))  # type: ignore[arg-type]

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
        cur = self.conn.cursor()
        cur.executemany(translate(sql), [adapt_params(p) for p in seq_of_params])  # type: ignore[arg-type]
        return cur

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        return self.execute(sql, params).fetchall()

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        row = self.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
        return int(row[0])

    @contextmanager
    def transaction(self) -> Iterator["PostgresStore"]:
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_WRITE,))
            yield self

    def ensure_schema(self) -> None:
        """Re-apply the idempotent schema (`db.init_schema` calls this)."""
        apply_schema(self.conn)

    def fts_rows(
        self, terms: list[str], tags: list[str], limit: int, with_snippet: bool
    ) -> list[Any]:
        return fts_rows(self, terms, tags, limit, with_snippet)

    def fts_covers(self, terms: list[str], chunk_id: int) -> bool:
        return fts_covers(self, terms, chunk_id)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            self._pool.putconn(conn)


# --- keyword search -------------------------------------------------------------
# A plain port so keyword mode and the semantic path's FTS union keep working on
# PostgreSQL. Phase 21 moves candidate retrieval behind kb/search/candidates.py
# and tunes it; nothing here is final.


def _tsquery(terms: list[str], op: str) -> str:
    """SQL building one tsquery from the terms: `plainto_tsquery` each, joined by `||` or `&&`."""
    return f" {op} ".join("plainto_tsquery('english', ?)" for _ in terms)


def fts_rows(
    store: PostgresStore, terms: list[str], tags: list[str], limit: int, with_snippet: bool
) -> list[Any]:
    """Top keyword matches by `ts_rank_cd` (higher is better); see `Store.fts_rows`."""
    if not terms:
        return []
    tag_sql, tag_params = tag_filter_sql(tags) if tags else ("", [])
    query = _tsquery(terms, "||")
    snippet = (
        f"ts_headline('english', c.text, {query},"
        " 'StartSel=<b>, StopSel=</b>, MaxWords=24, MinWords=10')"
        if with_snippet
        else "''"
    )
    snippet_params = list(terms) if with_snippet else []
    return store.fetchall(
        "SELECT c.id AS chunk_id, c.doc_id, c.heading_path, c.text, d.title, d.source_url,"
        f" d.verified_at, ts_rank_cd(c.tsv, q.query) AS score, {snippet} AS snip"
        f" FROM chunks c JOIN docs d ON d.id = c.doc_id, (SELECT {query} AS query) q"
        f" WHERE c.tsv @@ q.query{tag_sql} ORDER BY score DESC, c.id LIMIT ?",
        [*snippet_params, *terms, *tag_params, limit],
    )


def fts_covers(store: PostgresStore, terms: list[str], chunk_id: int) -> bool:
    """True if the chunk's `tsv` matches the AND of all `terms`."""
    if not terms:
        return False
    row = store.fetchone(
        f"SELECT 1 FROM chunks WHERE id = ? AND tsv @@ ({_tsquery(terms, '&&')})",
        [chunk_id, *terms],
    )
    return row is not None
