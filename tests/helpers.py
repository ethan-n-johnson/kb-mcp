"""Helpers shared by the search tests (fast and `models`)."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi.testclient import TestClient

from kb.config import Settings
from kb.store import Store, open_store
from tests.conftest import AUTH
from tests.corpus import DOCS


def upload(client: TestClient, md: str, tags: list[str] | None = None, **extra: Any) -> int:
    resp = client.post(
        "/api/docs", json={"content": md, "format": "markdown", "tags": tags or [], **extra}, headers=AUTH
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["doc_id"])


def load_corpus(client: TestClient) -> dict[str, int]:
    return {key: upload(client, md, tags) for key, (md, tags) in DOCS.items()}


def search(client: TestClient, q: str, **params: Any) -> dict[str, Any]:
    resp = client.get("/api/search", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def db_conn(client: TestClient) -> Store:
    return open_store(client.app.state.settings)


def reopen(db_path: object) -> Store:
    """A second connection to the database of the current backend (`backend` fixture).

    SQLite: the file at `db_path`. PostgreSQL (`KB_DATABASE_URL` set by the
    fixture): the same database, from the pool.
    """
    return open_store(dataclasses.replace(Settings.from_env(), db_path=str(db_path)))


def fts_count(conn: Store, word: str) -> int:
    """Number of chunks the full-text index finds `word` in (FTS5 table or `tsv` column)."""
    if conn.dialect == "postgres":
        sql = "SELECT count(*) FROM chunks WHERE tsv @@ plainto_tsquery('english', ?)"
    else:
        sql = "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?"
    return int(conn.execute(sql, (word,)).fetchone()[0])


def fts_integrity_check(conn: Store) -> None:
    """Raise if the SQLite FTS5 index no longer matches `chunks` (PostgreSQL derives `tsv` itself)."""
    if conn.dialect == "sqlite":
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('integrity-check')")


def gap_rows(client: TestClient) -> list[Any]:
    conn = db_conn(client)
    try:
        return conn.execute("SELECT * FROM gaps ORDER BY id").fetchall()
    finally:
        conn.close()
