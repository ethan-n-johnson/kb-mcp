"""SQLite dialect: the connection from `kb/db.py` plus the FTS5 keyword queries.

The connection class (`kb.db._Connection`) already is the SQLite `Store`: it
keeps `journal_mode=DELETE`, `foreign_keys=ON`, `busy_timeout` and the snapshot
commit hook. This module holds the SQL that only SQLite understands, which the
connection's `fts_rows` and `fts_covers` methods call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from kb import db
from kb.search.text import fts_match
from kb.store.base import tag_filter_sql


def open_sqlite(path: str | Path) -> sqlite3.Connection:
    """Open the SQLite database at `path` (see `kb.db.connect`)."""
    return db.connect(path)


def fts_rows(
    conn: sqlite3.Connection, terms: list[str], tags: list[str], limit: int, with_snippet: bool
) -> list[Any]:
    """Top FTS5 rows by BM25, best first; `score` is `-bm25()` so higher is better."""
    match = fts_match(terms)
    if not match:
        return []
    tag_sql, tag_params = tag_filter_sql(tags) if tags else ("", [])
    snippet_col = "snippet(chunks_fts, 1, '<b>', '</b>', '...', 24)" if with_snippet else "''"
    return conn.execute(
        f"SELECT c.id AS chunk_id, c.doc_id, c.heading_path, c.text, d.title, d.source_url,"
        f" d.verified_at, -bm25(chunks_fts) AS score, {snippet_col} AS snip"
        " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
        " JOIN docs d ON d.id = c.doc_id"
        f" WHERE chunks_fts MATCH ?{tag_sql} ORDER BY score DESC LIMIT ?",
        [match, *tag_params, limit],
    ).fetchall()


def fts_covers(conn: sqlite3.Connection, terms: list[str], chunk_id: int) -> bool:
    """True if the chunk matches every term, using FTS5's own tokenizer and stemmer."""
    if not terms:
        return False
    row = conn.execute(
        "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid = ?",
        (fts_match(terms, "AND"), chunk_id),
    ).fetchone()
    return row is not None
