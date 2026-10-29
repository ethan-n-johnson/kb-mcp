"""SQLite connection helpers, schema, FTS5 triggers and the commit hook.

This is the SQLite dialect's schema module. The PostgreSQL dialect lives in
`kb/store/` (`schema_pg.sql`, `postgres.py`); `transaction()` and
`init_schema()` here work on either kind of connection, and the SQLite
connection class also implements the rest of the `Store` interface
(`kb/store/base.py`: `insert`, `fetchone`, `fetchall`, `fts_rows`, ...).

The database lives on the instance root volume in production (`/data/kb.db`)
and is snapshotted to S3 (`kb/snapshot.py`). The rollback journal
(`journal_mode=DELETE`) is used and WAL is never enabled: one self-contained
file is what the snapshot copies and what a restore swaps in, with no `-wal` or
`-shm` sidecars to keep consistent.

The commit hook
---------------

`connect()` is the only way the application opens the database, and it returns
a `Connection` subclass that reports every committed write to the registered
commit listeners (`add_commit_listener`). Because the hook sits on the
connection, not on `transaction()` or on the service functions, it catches every
write route: explicit `BEGIN IMMEDIATE` transactions, statements that run in
autocommit mode outside any transaction, `executemany`, `executescript`,
cursors, and writes made by a search, a backfill or a trigger. See
`_Connection._after`.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

CommitListener = Callable[[str], None]
_listeners: list[CommitListener] = []
_listeners_lock = threading.Lock()


def add_commit_listener(listener: CommitListener) -> None:
    """Call `listener(db_key)` after every committed write on any connection from `connect()`.

    `db_key` is `db_key(path)` of the database that changed. Listeners run on
    the thread that committed, so they must be quick and must not raise (an
    exception is swallowed so a broken listener can never fail a request).
    """
    with _listeners_lock:
        _listeners.append(listener)


def remove_commit_listener(listener: CommitListener) -> None:
    """Unregister a listener added with `add_commit_listener` (no error if absent)."""
    with _listeners_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def db_key(path: str | Path) -> str:
    """Identify a database for the commit listeners: its resolved path, or `:memory:`."""
    if str(path) == ":memory:":
        return ":memory:"
    return str(Path(path).resolve())


def _notify(key: str) -> None:
    with _listeners_lock:
        current = list(_listeners)
    for listener in current:
        try:
            listener(key)
        except Exception:  # a listener must never break the write that triggered it
            pass


class _Cursor(sqlite3.Cursor):
    """Cursor whose statements go through the connection's commit hook."""

    def execute(self, sql: str, parameters: Any = (), /) -> "_Cursor":
        try:
            return super().execute(sql, parameters)  # type: ignore[return-value]
        finally:
            self.connection._after(sql)  # type: ignore[attr-defined]

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> "_Cursor":
        try:
            return super().executemany(sql, seq_of_parameters)  # type: ignore[return-value]
        finally:
            self.connection._after(sql)  # type: ignore[attr-defined]

    def executescript(self, sql_script: str, /) -> "_Cursor":
        try:
            return super().executescript(sql_script)  # type: ignore[return-value]
        finally:
            self.connection._after(sql_script)  # type: ignore[attr-defined]


class _Connection(sqlite3.Connection):
    """SQLite connection that tells the commit listeners when a write was committed.

    `total_changes` counts the rows inserted, updated or deleted on this
    connection (trigger and foreign key cascades included). After every
    statement that leaves the connection outside a transaction (an autocommit
    statement, or `COMMIT`), a count that moved since the last look means a
    write just became durable, and the listeners are told. Inside a
    transaction nothing is reported until it commits. A `ROLLBACK` discards the
    pending count instead of reporting it. A listener can therefore be told
    "something changed" a little too often (an `UPDATE` that matches no row is
    silent, but a write that changes a row to the same value is not) and never
    too rarely.
    """

    dialect = "sqlite"
    _seen_changes: int = 0
    _key: str = ""
    _quiet: bool = False

    # --- the rest of the Store interface (kb/store/base.py) ---

    def fetchone(self, sql: str, params: Any = ()) -> Any:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Any = ()) -> list[Any]:
        return self.execute(sql, params).fetchall()

    def insert(self, sql: str, params: Any = ()) -> int:
        """Run an `INSERT` and return the new row's id."""
        return int(self.execute(sql, params).lastrowid or 0)

    def transaction(self) -> Any:
        """Context manager: `BEGIN IMMEDIATE`, then commit, or roll back on an exception."""
        return _sqlite_transaction(self)

    def fts_rows(self, terms: list[str], tags: list[str], limit: int, with_snippet: bool) -> list[Any]:
        from kb.store import sqlite as dialect

        return dialect.fts_rows(self, terms, tags, limit, with_snippet)

    def fts_covers(self, terms: list[str], chunk_id: int) -> bool:
        from kb.store import sqlite as dialect

        return dialect.fts_covers(self, terms, chunk_id)

    def _after(self, sql: object = "") -> None:
        """Compare `total_changes` with the last look; report a committed write."""
        if self.in_transaction:
            return  # not durable yet; the COMMIT statement reports it
        total = self.total_changes
        changed = total != self._seen_changes
        self._seen_changes = total
        rolled_back = isinstance(sql, str) and sql.lstrip()[:8].upper() == "ROLLBACK"
        if changed and not rolled_back and not self._quiet:
            _notify(self._key)

    def cursor(self, factory: Any = None) -> sqlite3.Cursor:  # type: ignore[override]
        return super().cursor(factory or _Cursor)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        try:
            return super().execute(sql, parameters)
        finally:
            self._after(sql)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        try:
            return super().executemany(sql, parameters)
        finally:
            self._after(sql)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        try:
            return super().executescript(sql_script)
        finally:
            self._after(sql_script)

    def commit(self) -> None:
        try:
            super().commit()
        finally:
            self._after("COMMIT")


def utcnow() -> str:
    """Return the current time as an ISO 8601 UTC string (seconds precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database at `path`, creating the parent directory if missing.

    The connection is in autocommit mode (`isolation_level=None`) so that
    `transaction()` controls `BEGIN`/`COMMIT` explicitly. Pragmas:
    `journal_mode=DELETE`, `foreign_keys=ON`, `busy_timeout=5000`. Rows are
    `sqlite3.Row`. `check_same_thread=False` lets the thread pool share it;
    callers must not interleave transactions from several threads. Committed
    writes are reported to the commit listeners (see the module docstring).
    """
    db_path = Path(path)
    if str(path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        check_same_thread=False,
        timeout=5.0,
        factory=_Connection,
    )
    conn._key = db_key(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def transaction(conn: Any) -> Any:
    """Run a block in a write transaction on either kind of connection (a context manager).

    Commits on success and rolls back on an exception. SQLite: `BEGIN IMMEDIATE`.
    PostgreSQL: a transaction guarded by an advisory lock (`PostgresStore.transaction`).
    """
    method = getattr(conn, "transaction", None)
    return method() if method is not None else _sqlite_transaction(conn)


@contextmanager
def _sqlite_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside `BEGIN IMMEDIATE`; commit on success, roll back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    source_url    TEXT,
    source_type   TEXT NOT NULL DEFAULT 'markdown',
    content_md    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    s3_key        TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    verified_at   TEXT,
    verified_by   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_content_hash ON docs(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    ord           INTEGER NOT NULL,
    heading_path  TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL,
    embedding     BLOB,
    embed_model   TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    heading_path, text,
    content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, heading_path, text)
    VALUES (new.id, new.heading_path, new.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading_path, text)
    VALUES ('delete', old.id, old.heading_path, old.text);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading_path, text)
    VALUES ('delete', old.id, old.heading_path, old.text);
    INSERT INTO chunks_fts(rowid, heading_path, text)
    VALUES (new.id, new.heading_path, new.text);
END;

CREATE TABLE IF NOT EXISTS doc_tags (
    doc_id  INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag);

CREATE TABLE IF NOT EXISTS gaps (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query            TEXT NOT NULL,
    query_norm       TEXT NOT NULL,
    top_score        REAL,
    mode             TEXT,
    created_at       TEXT NOT NULL,
    resolved_doc_id  INTEGER REFERENCES docs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_query_norm ON gaps(query_norm);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token_name  TEXT,
    action      TEXT NOT NULL,
    doc_id      INTEGER,
    at          TEXT NOT NULL,
    note        TEXT
);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table was first created (existing databases).

    `CREATE TABLE IF NOT EXISTS` leaves an older table untouched, so columns
    added later are applied here with `ALTER TABLE`. Currently: `audit_log.note`
    (phase 08, the `verify` note).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_log)")}
    if "note" not in columns:
        try:
            conn.execute("ALTER TABLE audit_log ADD COLUMN note TEXT")
        except sqlite3.OperationalError as exc:  # another process added it first
            if "duplicate column" not in str(exc).lower():
                raise


def init_schema(conn: Any) -> None:
    """Create all tables, indexes, the FTS5 table and its triggers (idempotent).

    On a PostgreSQL store this re-applies `kb/store/schema_pg.sql` instead.

    Also upgrades databases created by earlier phases (see `_add_missing_columns`).
    Creating the FTS5 tables inserts a few internal rows; those are schema setup,
    not data, so they are not reported to the commit listeners (an empty database
    must not become a snapshot just because the app started on it).
    """
    if getattr(conn, "dialect", "sqlite") == "postgres":
        conn.ensure_schema()
        return
    quiet = isinstance(conn, _Connection)
    if quiet:
        conn._quiet = True  # type: ignore[attr-defined]
    try:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
    finally:
        if quiet:
            conn._quiet = False  # type: ignore[attr-defined]
