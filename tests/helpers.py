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


def fresh_health(client: TestClient) -> dict[str, Any]:
    """`/healthz` JSON after a new database count.

    In PostgreSQL mode `/healthz` serves the counts a monitor thread cached up to
    10 s ago (`kb.health.HealthMonitor`), so a test that wrote and wants the new
    counts asks the monitor for a check first. SQLite mode counts per request.
    """
    monitor = getattr(client.app.state, "health", None)
    if monitor is not None:
        assert monitor.check_once()
    return client.get("/healthz").json()


def db_conn(client: TestClient) -> Store:
    return open_store(client.app.state.settings)


def searchable_doc_ids(state: Any, conn: Store) -> set[int]:
    """Ids of the documents the semantic search can reach.

    SQLite: the documents in the in-memory index. PostgreSQL has no index (the
    vectors are searched in the database), so: the documents with a stored
    vector from the current model.
    """
    if state.settings.dialect == "sqlite":
        return set(state.index.doc_ids())
    rows = conn.execute(
        "SELECT DISTINCT doc_id FROM chunks WHERE embedding IS NOT NULL AND embed_model = ?",
        (state.embedder.model_name,),
    ).fetchall()
    return {int(r[0]) for r in rows}


def client_searchable_doc_ids(client: TestClient) -> set[int]:
    """`searchable_doc_ids` for the app behind `client`."""
    conn = db_conn(client)
    try:
        return searchable_doc_ids(client.app.state.search, conn)
    finally:
        conn.close()


def searchable_chunk_count(state: Any, conn: Store) -> int:
    """Number of vectors the semantic search can reach (see `searchable_doc_ids`)."""
    if state.settings.dialect == "sqlite":
        return len(state.index)
    row = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()
    return int(row[0])


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
