"""Helpers shared by the search tests (fast and `models`)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from kb import db
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


def db_conn(client: TestClient) -> sqlite3.Connection:
    return db.connect(client.app.state.settings.db_path)


def gap_rows(client: TestClient) -> list[sqlite3.Row]:
    conn = db_conn(client)
    try:
        return conn.execute("SELECT * FROM gaps ORDER BY id").fetchall()
    finally:
        conn.close()
