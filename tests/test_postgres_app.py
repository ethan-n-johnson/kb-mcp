"""The application on a PostgreSQL database (`pg` tests; skipped without `KB_TEST_DATABASE_URL`).

Search on PostgreSQL is phase 21: here the semantic path still uses the in-memory
index loaded from the database, and keyword mode a plain `tsvector` query. These
tests pin that the app starts, answers, restarts and writes on PostgreSQL.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS, QUERIES
from tests.helpers import db_conn, load_corpus, search, upload

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _postgres(pg_store):  # noqa: ANN001 - fixture that selects the PostgreSQL leg for open_app
    yield pg_store


def test_healthz_reports_chunks_and_holds_no_connection(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.app.state.conn is None
        assert client.get("/healthz").json()["chunks"] == 0
        load_corpus(client)
        body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["mode"] == "semantic"
    assert body["chunks"] > 0 and body["index_size"] == body["chunks"]


def test_semantic_search_answers_from_postgres(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        for query, key, suffix in QUERIES.values():
            body = search(client, query)
            assert body["mode"] == "semantic"
            top = body["results"][0]
            assert top["doc_id"] == ids[key] and top["heading_path"].endswith(suffix)


def test_keyword_mode_answers_from_postgres(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        ids = load_corpus(client)
        body = search(client, "NAT gateway private subnet internet")
        assert body["mode"] == "keyword"
        assert body["results"][0]["doc_id"] == ids["vpc"]
        assert "<b>" in body["results"][0]["snippet"]
        tagged = search(client, "storage volumes buckets", tags="networking")
        assert all(r["doc_id"] in (ids["elb"], ids["vpc"]) for r in tagged["results"])


def test_an_unanswerable_question_logs_a_gap(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        search(client, "xylophone quasar zeppelin")
        conn = db_conn(client)
        try:
            assert conn.fetchone("SELECT count(*) AS n FROM gaps")["n"] == 1
        finally:
            conn.close()


def test_a_restart_rebuilds_the_index_from_the_stored_vectors(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        first = client.get("/healthz").json()["index_size"]
    embedder_calls = []
    from tests.fakes import FakeEmbedder

    class Counting(FakeEmbedder):
        def embed_passages(self, texts):  # type: ignore[no-untyped-def]
            embedder_calls.append(len(texts))
            return super().embed_passages(texts)

    with open_app(Counting()) as client:
        assert client.get("/healthz").json()["index_size"] == first > 0
        assert search(client, QUERIES["nat"][0])["results"][0]["doc_id"] == ids["vpc"]
    assert embedder_calls == []  # nothing was re-embedded


def test_snapshots_never_start_in_postgres_mode(open_app: OpenApp) -> None:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="kb-x")
        with open_app(models=False, KB_BUCKET="kb-x") as client:
            assert client.app.state.snapshots is None
            upload(client, DOCS["s3"][0])
        s3 = boto3.client("s3", region_name="us-east-1")
        assert "Contents" not in s3.list_objects_v2(Bucket="kb-x", Prefix="snapshots/")


def test_mcp_tools_write_and_read_on_postgres(open_app: OpenApp) -> None:
    from tests.mcp_helpers import call, ok

    with open_app(models=False) as client:
        created = ok(call(client, "upload", {"title": "From MCP", "content": "# From MCP\n\n" + "word " * 60}))
        assert created["status"] == "created"
        got = ok(call(client, "get", {"doc_id": created["doc_id"]}))
        assert got["title"] == "From MCP"
        audit = client.get("/api/audit").json()["entries"]
    assert (audit[0]["token_name"], audit[0]["action"]) == ("mcp", "upload")


def test_rest_write_lands_with_the_token_name(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        doc = upload(client, DOCS["ebs"][0], ["storage"])
        assert client.get(f"/api/docs/{doc}").json()["tags"] == ["storage"]
        assert client.delete(f"/api/docs/{doc}", headers=AUTH).status_code == 200
        assert client.get(f"/api/docs/{doc}").status_code == 404


def test_inspect_searches_a_temporary_export_and_writes_nothing_back(
    open_app: OpenApp, pg_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kb.cli search` and `eval` in PostgreSQL mode: a gap logged by the search stays in the copy."""
    from kb import evaluate
    from kb.config import Settings
    from tests.fakes import FakeEmbedder, FakeReranker

    with open_app() as client:
        load_corpus(client)
    settings = Settings.from_env()
    assert settings.dialect == "postgres"
    out = evaluate.inspect(
        settings,
        [QUERIES["nat"][0], "xylophone quasar zeppelin"],
        embedder=FakeEmbedder(),
        reranker=FakeReranker(),
    )
    assert "[semantic, high]" in out and "NAT gateway" in out
    assert pg_store.fetchone("SELECT count(*) AS n FROM gaps")["n"] == 0
