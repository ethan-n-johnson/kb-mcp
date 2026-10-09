"""Helpers shared by the phase 08 tests (fast and `models`)."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

import httpx
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.corpus import DOCS, EBS
from tests.fakes import FakeEmbedder

# A lightly edited copy of the EBS doc: one sentence reworded, everything else identical.
EBS_EDITED = EBS.replace(
    "Only blocks that changed since the previous snapshot are saved, which keeps the cost low.",
    "After the first snapshot only the changed blocks are saved, so the cost stays low.",
)
assert EBS_EDITED != EBS

LINUX_QUERY = "How do I attach an EBS volume to a Linux instance?"


def post_doc(client: TestClient, md: str, **extra: Any) -> httpx.Response:
    """`POST /api/docs` with the test token (no status assertion)."""
    body = {"content": md, "format": "markdown", **extra}
    return client.post("/api/docs", json=body, headers=AUTH)


def get_doc(client: TestClient, doc_id: int, chunks: bool = True) -> dict[str, Any]:
    resp = client.get(f"/api/docs/{doc_id}", params={"include_chunks": str(chunks).lower()})
    assert resp.status_code == 200, resp.text
    return resp.json()


def rows(client: TestClient, sql: str, *params: Any) -> list[sqlite3.Row]:
    """Run a read query on a fresh connection to the app's database."""
    from tests.helpers import db_conn

    conn = db_conn(client)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def scalar(client: TestClient, sql: str, *params: Any) -> Any:
    return rows(client, sql, *params)[0][0]


def audit_rows(client: TestClient) -> list[tuple[str, str, int | None]]:
    """`(token_name, action, doc_id)` for every audit row, oldest first."""
    return [
        (r["token_name"], r["action"], r["doc_id"])
        for r in rows(client, "SELECT token_name, action, doc_id FROM audit_log ORDER BY id")
    ]


def backdate(client: TestClient, doc_id: int, days: int, *, column: str = "updated_at") -> None:
    """Set a doc's `updated_at` (or `verified_at`) to `days` days ago."""
    from tests.helpers import db_conn

    conn = db_conn(client)
    try:
        conn.execute(
            f"UPDATE docs SET {column} = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) WHERE id = ?",
            (f"-{days} days", doc_id),
        )
    finally:
        conn.close()


def add_gap(
    client: TestClient,
    query: str,
    norm: str | None = None,
    *,
    top_score: float | None = None,
    mode: str = "semantic",
    at: str = "2026-10-01T10:00:00Z",
    resolved_doc_id: int | None = None,
) -> None:
    """Insert one `gaps` row directly (for tests that need exact counts and times)."""
    from tests.helpers import db_conn

    conn = db_conn(client)
    try:
        conn.execute(
            "INSERT INTO gaps (query, query_norm, top_score, mode, created_at, resolved_doc_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (query, norm or query.lower(), top_score, mode, at, resolved_doc_id),
        )
    finally:
        conn.close()


class ProbeEmbedder(FakeEmbedder):
    """Fake embedder that runs `on_embed()` at the start of every passage embedding.

    Lets a test look at the database, or change it from another connection,
    while the service is embedding (that is, before its write transaction).
    """

    def __init__(self, on_embed: Callable[[], None] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.on_embed = on_embed

    def embed_passages(self, texts: Any, batch_size: int = 64) -> Any:
        if self.on_embed is not None:
            self.on_embed()
        return super().embed_passages(texts, batch_size)


def doc_ids(client: TestClient) -> list[int]:
    return [r["id"] for r in rows(client, "SELECT id FROM docs ORDER BY id")]


__all__ = [
    "DOCS",
    "EBS_EDITED",
    "LINUX_QUERY",
    "ProbeEmbedder",
    "add_gap",
    "audit_rows",
    "backdate",
    "doc_ids",
    "get_doc",
    "post_doc",
    "rows",
    "scalar",
]
