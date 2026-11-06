"""The application on a PostgreSQL database (`pg` tests; skipped without `KB_TEST_DATABASE_URL`).

In PostgreSQL mode candidates come from the database (pgvector and `tsvector`,
`kb/search/candidates.py`) and no in-memory index is built. These tests pin that
the app starts, answers, restarts and writes on PostgreSQL, and the parts of
search that exist only there: the HNSW index, the empty numpy index, the tag
filter and the document vectors.
"""

from __future__ import annotations

import boto3
import numpy as np
import pytest
from moto import mock_aws

from kb.search import candidates
from kb.search.index import VectorIndex
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


def test_a_restart_reports_the_stored_vectors_without_re_embedding(open_app: OpenApp) -> None:
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


# --- candidates from the database ---------------------------------------------------


def test_the_hnsw_index_exists(pg_store) -> None:  # noqa: ANN001
    row = pg_store.fetchone(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'chunks' AND indexname = 'idx_chunks_embedding'"
    )
    assert row is not None
    assert "hnsw" in row[0] and "vector_cosine_ops" in row[0]


def test_startup_builds_no_numpy_index_and_healthz_counts_embedded_chunks(
    open_app: OpenApp,
) -> None:
    with open_app() as client:
        state = client.app.state.search
        assert len(state.index) == 0
        load_corpus(client)
        assert len(state.index) == 0  # writes do not fill it either
        conn = db_conn(client)
        try:
            embedded = conn.fetchone("SELECT count(*) AS n FROM chunks WHERE embedding IS NOT NULL")["n"]
        finally:
            conn.close()
        body = client.get("/healthz").json()
    assert embedded > 0 and body["index_size"] == embedded
    with open_app() as client:  # a restart: still nothing in memory, the count comes from the table
        assert len(client.app.state.search.index) == 0
        assert client.get("/healthz").json()["index_size"] == embedded


def _numpy_index(conn, model: str) -> VectorIndex:  # noqa: ANN001
    index = VectorIndex()
    index.load_from_db(conn, model)
    return index


def test_vector_candidates_match_the_numpy_index_and_respect_tags(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        state = client.app.state.search
        qvec = state.embedder.embed_query(QUERIES["nat"][0])
        conn = db_conn(client)
        try:
            index = _numpy_index(conn, state.embedder.model_name)
            plain = candidates.vector_candidates(conn, state, qvec, 6, [])
            expected = index.top_k(qvec, 6)
            assert [c[0] for c in plain] == [c[0] for c in expected]
            np.testing.assert_allclose([c[2] for c in plain], [c[2] for c in expected], atol=1e-5)
            tagged = candidates.vector_candidates(conn, state, qvec, 50, ["networking"])
            tagged_docs = {r["doc_id"] for r in conn.execute(
                "SELECT doc_id FROM doc_tags WHERE tag = 'networking'"
            )}
            assert tagged and {c[1] for c in tagged} <= tagged_docs
            assert {c[1] for c in tagged} < {c[1] for c in candidates.vector_candidates(conn, state, qvec, 50, [])}
            assert ids["vpc"] in tagged_docs
            assert candidates.vector_candidates(conn, state, qvec, 5, ["no-such-tag"]) == []
        finally:
            conn.close()


def test_a_vector_from_another_model_is_not_a_candidate(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        state = client.app.state.search
        qvec = state.embedder.embed_query(QUERIES["nat"][0])
        conn = db_conn(client)
        try:
            conn.execute("UPDATE chunks SET embed_model = 'other-model'")
            assert candidates.vector_candidates(conn, state, qvec, 5, []) == []
            assert candidates.doc_vectors(conn, state)[0].shape == (0,)
        finally:
            conn.close()


def test_doc_vectors_equal_the_numpy_index_result(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        state = client.app.state.search
        conn = db_conn(client)
        try:
            ids, matrix = candidates.doc_vectors(conn, state)
            want_ids, want = _numpy_index(conn, state.embedder.model_name).doc_vectors()
        finally:
            conn.close()
    assert ids.tolist() == want_ids.tolist() and len(ids) > 1
    assert matrix.shape == want.shape and matrix.dtype == np.float32
    np.testing.assert_allclose(matrix, want, atol=1e-5)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


def test_eval_on_postgres_searches_the_database_and_leaves_no_gaps(
    open_app: OpenApp, pg_store, tmp_path
) -> None:  # noqa: ANN001
    """`kb.cli eval` measures pgvector and `tsvector` itself, and removes the gaps it logged."""
    from kb.config import Settings
    from kb.evaluate import Question, evaluate
    from tests.corpus import EBS
    from tests.fakes import FakeEmbedder, FakeReranker

    url = "https://docs.example.com/ebs.html"
    with open_app() as client:
        upload(client, EBS, source_url=url)
        upload(client, DOCS["s3"][0], source_url="https://docs.example.com/s3.html")
    planted = tmp_path / "copy.md"
    planted.write_text(EBS.replace("Amazon Elastic Block Store", "Our notes on Amazon EBS"), encoding="utf-8")
    pg_store.execute("INSERT INTO gaps (query, query_norm, mode, created_at) VALUES ('old', 'old', 'keyword', now())")
    questions = [
        Question("attach an EBS volume to a Linux instance mount", "answerable", (url,)),
        Question("sourdough bread recipe", "outside"),
    ]
    report = evaluate(
        Settings.from_env(), questions, planted=planted, planted_original=url,
        embedder=FakeEmbedder(), reranker=FakeReranker(),
    )
    assert report.results[0].rank == 1 and report.results[0].mode == "semantic"
    assert report.dup is not None and report.dup.top_candidate_url == url
    assert report.dup.original_similarity is not None and report.dup.original_similarity > 0.9
    # The outside question logged a gap while it ran; only the earlier row remains.
    assert pg_store.fetchone("SELECT count(*) AS n FROM gaps")["n"] == 1
