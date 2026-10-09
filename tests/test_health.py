"""/healthz contract."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "mode": "keyword",
        "models": {"embedder": "off", "reranker": "off"},
        "fallback_toggle": False,
        "chunks": 0,
        "index_size": 0,
    }


def test_healthz_counts_chunks(client: TestClient) -> None:
    conn = client.app.state.conn
    conn.execute(
        "INSERT INTO docs(title, source_type, content_md, content_hash, created_at, updated_at)"
        " VALUES ('T', 'markdown', 'b', 'x', 'n', 'n')"
    )
    conn.execute("INSERT INTO chunks(doc_id, ord, heading_path, text) VALUES (1, 0, 'h', 't')")
    assert client.get("/healthz").json()["chunks"] == 1


def test_healthz_503_when_db_unreadable(client: TestClient) -> None:
    client.app.state.conn.close()
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
