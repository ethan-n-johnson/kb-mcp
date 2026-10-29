"""The `Store` interface shared by the SQLite and PostgreSQL backends.

Every function in `kb/` that touches the database takes a `Store`. Statements
are written once with `?` placeholders; the PostgreSQL store translates them
(`kb/store/postgres.py`). Rows behave like `sqlite3.Row` in both dialects:
`row["name"]`, `row[0]`, `row.keys()`, iteration over values.

SQL that only one dialect understands (the schema, full-text search, vector
queries) lives in that dialect's module and is reached through the store's own
methods (`fts_rows`, `fts_covers`), never by branching in `service.py`.
"""

from __future__ import annotations

from typing import Any, ContextManager, Iterable, Literal, Protocol, Sequence

Dialect = Literal["sqlite", "postgres"]


class Result(Protocol):
    """What `Store.execute` returns (a `sqlite3.Cursor` or a psycopg cursor)."""

    rowcount: int

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Any: ...


class Store(Protocol):
    """A database connection, SQLite or PostgreSQL, with the operations `kb/` uses."""

    dialect: Dialect

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Run one statement; the result has `fetchone`, `fetchall`, `rowcount` and iterates rows."""

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
        """Run one statement once per parameter tuple."""

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """First row of a query, or None."""

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        """All rows of a query."""

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an `INSERT` into a table with an integer `id` and return the new id."""

    def transaction(self) -> ContextManager[Any]:
        """Context manager: all writes inside commit together, or roll back on an exception.

        Write transactions are serialized (SQLite: `BEGIN IMMEDIATE`; PostgreSQL:
        a transaction-scoped advisory lock), so a read-check-write sequence inside
        one cannot interleave with another writer.
        """

    def fts_rows(
        self, terms: list[str], tags: list[str], limit: int, with_snippet: bool
    ) -> list[Any]:
        """Top keyword matches, best first.

        Rows carry `chunk_id`, `doc_id`, `heading_path`, `text`, `title`,
        `source_url`, `verified_at`, `score` (higher is better) and `snip`
        (`<b>`-marked excerpt, empty unless `with_snippet`). `terms` are
        OR-ed; no terms means no rows.
        """

    def fts_covers(self, terms: list[str], chunk_id: int) -> bool:
        """True if the chunk matches every one of `terms` (the dialect's own tokenizer and stemmer)."""

    def close(self) -> None:
        """Release the connection (PostgreSQL: back to the pool)."""


def tag_filter_sql(tags: list[str]) -> tuple[str, list[object]]:
    """SQL fragment (on `c.doc_id`) keeping only docs that carry ALL `tags`."""
    marks = ",".join("?" * len(tags))
    sql = (
        " AND c.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag IN (" + marks + ")"
        " GROUP BY doc_id HAVING count(DISTINCT tag) = ?)"
    )
    return sql, [*tags, len(tags)]
