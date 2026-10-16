"""Search pipeline end to end with the fake embedder and reranker.

Every semantic code path runs here without real models: top-k, FTS union,
rerank, the 2-per-doc cap, confidence, timeout and error fallback, the toggle,
backfill, rebuild without re-embedding, delete, tags and `KB_RERANK=off`.
`tests/test_search_models.py` repeats the key scenarios with the real models.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kb.config import Settings
from kb.search import pipeline
from kb.search.embedder import passage_text
from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS, QUERIES
from tests.helpers import db_conn, gap_rows, load_corpus, search, upload
from tests.fakes import FakeEmbedder, FakeReranker


class ConstReranker(FakeReranker):
    """Gives every passage the same score, or a score chosen by a marker word."""

    def __init__(self, value: float = 0.5, by_marker: dict[str, float] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.value = value
        self.by_marker = by_marker or {}

    def score(self, query: str, texts: Sequence[str]) -> np.ndarray:
        self.score_calls.append((query, list(texts)))
        out = []
        for text in texts:
            hit = [v for marker, v in self.by_marker.items() if marker in text]
            out.append(hit[0] if hit else self.value)
        return np.asarray(out, dtype=np.float32)


# --- semantic path ----------------------------------------------------------------


@pytest.mark.parametrize("name", list(QUERIES))
def test_semantic_search_finds_the_right_chunk_at_rank_one(open_app: OpenApp, name: str) -> None:
    query, doc_key, path_suffix = QUERIES[name]
    with open_app() as client:
        ids = load_corpus(client)
        body = search(client, query)
    assert body["mode"] == "semantic"
    top = body["results"][0]
    assert top["doc_id"] == ids[doc_key]
    assert top["heading_path"].endswith(path_suffix)
    assert set(top) == {
        "doc_id", "chunk_id", "title", "heading_path", "snippet", "score", "source_url", "verified_at"
    }
    assert 0.0 < top["score"] < 1.0
    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)


def test_response_carries_doc_metadata(open_app: OpenApp) -> None:
    with open_app() as client:
        md, _ = DOCS["ebs"]
        doc_id = upload(client, md, source_url="https://docs.example.com/ebs")
        conn = db_conn(client)
        conn.execute("UPDATE docs SET verified_at = '2026-10-10' WHERE id = ?", (doc_id,))
        conn.close()
        top = search(client, QUERIES["ebs_linux"][0])["results"][0]
    assert top["title"] == "Amazon EBS volumes"
    assert top["source_url"] == "https://docs.example.com/ebs"
    assert top["verified_at"] == "2026-10-10"
    assert len(top["snippet"]) <= 240 and "<b>" not in top["snippet"]
    assert "To attach an EBS volume to a Linux instance" in top["snippet"]


def test_k_limits_results_and_is_capped_at_20(open_app: OpenApp) -> None:
    with open_app() as client:
        for n in range(25):
            upload(client, f"# Zebra note {n}\n\n" + "zebra stripes herd savanna " * 15 + f"unique{n}")
        assert len(search(client, "zebra", k=3)["results"]) == 3
        assert len(search(client, "zebra", k=100)["results"]) == 20
        assert len(search(client, "zebra")["results"]) == 5


def test_at_most_two_chunks_per_doc(open_app: OpenApp) -> None:
    sections = "\n\n".join(
        f"## Part {n}\n\n" + "scaling groups launch instances automatically " * 12 for n in range(5)
    )
    with open_app() as client:
        big = upload(client, "# Auto Scaling\n\n" + sections)
        other = upload(client, "# Other\n\n" + "scaling groups launch instances automatically " * 12)
        body = search(client, "scaling groups launch instances", k=10)
        keyword_body = search(client, "scaling groups launch instances", k=10)
    assert body["mode"] == "semantic"
    per_doc = [r["doc_id"] for r in body["results"]]
    assert per_doc.count(big) == 2 and per_doc.count(other) == 1
    assert len(body["results"]) == 3
    assert [r["doc_id"] for r in keyword_body["results"]].count(big) == 2


def test_rerank_order_overrides_cosine_order(open_app: OpenApp) -> None:
    """The final order follows the reranker, even against the cosine order."""
    reranker = ConstReranker(0.2, by_marker={"Windows": 0.9})
    with open_app(reranker=reranker) as client:
        load_corpus(client)
        body = search(client, QUERIES["ebs_linux"][0])
    assert body["results"][0]["heading_path"].endswith("Windows")
    assert body["results"][0]["score"] == pytest.approx(0.9, abs=1e-4)
    assert reranker.score_calls, "the reranker must have been called"
    # It receives the same text that was embedded: title, heading path and chunk text.
    assert any(t.startswith("Amazon EBS volumes > Attach a volume > Linux\n\n") for t in reranker.score_calls[0][1])


def test_fts_union_adds_chunks_outside_the_cosine_pool(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline, "SEMANTIC_CANDIDATES", 1)

    class OddEmbedder(FakeEmbedder):
        """Queries embed to a fixed vector unrelated to the corpus, so cosine ranks by luck."""

        def embed_query(self, query: str) -> np.ndarray:
            return super().embed(["zzz qqq"])[0]

    reranker = FakeReranker()
    with open_app(OddEmbedder(), reranker) as client:
        ids = load_corpus(client)
        body = search(client, "VcpuLimitExceeded quota")
    assert body["results"][0]["doc_id"] == ids["errors"]
    assert len(reranker.score_calls[0][1]) >= 2  # 1 cosine candidate plus FTS hits


# --- confidence and gaps ----------------------------------------------------------


@pytest.mark.parametrize(
    "score, expected", [(0.95, "high"), (0.70, "high"), (0.69, "medium"), (0.30, "medium"), (0.29, "low")]
)
def test_semantic_confidence_boundaries(open_app: OpenApp, score: float, expected: str) -> None:
    with open_app(reranker=ConstReranker(score)) as client:
        load_corpus(client)
        body = search(client, QUERIES["nat"][0])
    assert body["mode"] == "semantic"
    assert body["confidence"] == expected


def test_confidence_uses_env_cutoffs(open_app: OpenApp) -> None:
    with open_app(reranker=ConstReranker(0.5), KB_CONF_HIGH="0.4", KB_CONF_LOW="0.1") as client:
        load_corpus(client)
        assert search(client, QUERIES["nat"][0])["confidence"] == "high"


def test_low_semantic_result_writes_a_gap_row(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        body = search(client, "Airspeed velocity of unladen swallow?")
        rows = gap_rows(client)
    assert body["mode"] == "semantic" and body["confidence"] == "low"
    assert len(rows) == 1
    row = rows[0]
    assert row["query"] == "Airspeed velocity of unladen swallow?"
    assert row["query_norm"] == "airspeed velocity unladen swallow"
    assert row["mode"] == "semantic"
    assert row["top_score"] == pytest.approx(body["results"][0]["score"], abs=1e-4)
    assert row["resolved_doc_id"] is None


def test_confident_results_do_not_write_gaps(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        search(client, QUERIES["ebs_linux"][0])
        assert gap_rows(client) == []


def test_no_results_is_low_with_null_top_score(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        body = search(client, "nothing matches this")
        rows = gap_rows(client)
    assert body == {"mode": "keyword", "confidence": "low", "results": []}
    assert rows[0]["top_score"] is None and rows[0]["mode"] == "keyword"


# --- fallback: timeout, errors, health, toggle --------------------------------------


def test_slow_embedder_times_out_and_falls_back_to_keyword(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder, KB_SEARCH_TIMEOUT="0.3") as client:
        load_corpus(client)
        embedder.delay = 2.0
        started = time.perf_counter()
        body = search(client, "attach EBS volume Linux")
        elapsed = time.perf_counter() - started
    assert body["mode"] == "keyword"
    assert body["results"], "keyword search still answers"
    # The fallback must not wait for the abandoned model thread to finish.
    assert elapsed < 1.5


def test_slow_reranker_times_out_too(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_SEARCH_TIMEOUT="0.3") as client:
        load_corpus(client)
        reranker.delay = 2.0
        started = time.perf_counter()
        body = search(client, "attach EBS volume Linux")
    assert body["mode"] == "keyword"
    assert time.perf_counter() - started < 1.5


def test_embedder_error_falls_back_to_keyword(
    open_app: OpenApp, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        load_corpus(client)
        embedder.fail = True
        with caplog.at_level(logging.ERROR):
            body = search(client, "attach EBS volume Linux")
    assert body["mode"] == "keyword" and body["results"]
    assert any("semantic search failed" in r.getMessage() for r in caplog.records)


def test_reranker_error_falls_back_to_keyword(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        load_corpus(client)
        reranker.fail = True
        assert search(client, "attach EBS volume Linux")["mode"] == "keyword"
        reranker.fail = False
        assert search(client, "attach EBS volume Linux")["mode"] == "semantic"


def test_unhealthy_models_mean_keyword_mode_and_healthz_stays_200(open_app: OpenApp) -> None:
    embedder, reranker = FakeEmbedder(), FakeReranker()
    with open_app(embedder, reranker) as client:
        load_corpus(client)
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["mode"] == "semantic"
        assert health.json()["models"] == {"embedder": "ok", "reranker": "ok"}
        reranker.healthy = False
        assert search(client, "attach EBS volume Linux")["mode"] == "keyword"
        health = client.get("/healthz")
        assert health.status_code == 200
        assert (health.json()["mode"], health.json()["models"]["reranker"]) == ("keyword", "failed")
        reranker.healthy, embedder.healthy = True, False
        assert client.get("/healthz").json()["mode"] == "keyword"


def test_models_that_fail_to_load_leave_the_server_in_keyword_mode(open_app: OpenApp) -> None:
    with open_app(FakeEmbedder(healthy=False), FakeReranker(healthy=False)) as client:
        md, tags = DOCS["ebs"]
        upload(client, md, tags)
        health = client.get("/healthz")
        body = search(client, "attach EBS volume Linux")
    assert health.status_code == 200
    assert health.json()["mode"] == "keyword"
    assert health.json()["models"] == {"embedder": "failed", "reranker": "failed"}
    assert body["mode"] == "keyword"


def test_real_model_classes_degrade_gracefully_when_loading_fails(open_app: OpenApp) -> None:
    from kb.search.embedder import Embedder
    from kb.search.reranker import Reranker

    def boom(name: str, cache_dir: str | None = None) -> Any:
        raise OSError("model cache missing")

    with open_app(Embedder(model_factory=boom), Reranker(model_factory=boom)) as client:  # type: ignore[arg-type]
        health = client.get("/healthz")
        md, tags = DOCS["ebs"]
        upload(client, md, tags)
        body = search(client, "attach EBS volume Linux")
    assert health.status_code == 200
    assert health.json()["mode"] == "keyword"
    assert health.json()["models"] == {"embedder": "failed", "reranker": "failed"}
    assert body["mode"] == "keyword" and body["results"]


def test_toggle_forces_keyword_and_back(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        q = QUERIES["ebs_linux"][0]
        assert search(client, q)["mode"] == "semantic"

        assert client.post("/api/admin/fallback", json={"on": True}).status_code == 401
        assert client.get("/healthz").json()["fallback_toggle"] is False

        resp = client.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        assert resp.status_code == 200 and resp.json() == {"fallback_toggle": True}
        assert search(client, q)["mode"] == "keyword"
        health = client.get("/healthz").json()
        assert (health["mode"], health["fallback_toggle"]) == ("keyword", True)
        assert health["models"] == {"embedder": "ok", "reranker": "ok"}

        client.post("/api/admin/fallback", json={"on": False}, headers=AUTH)
        assert search(client, q)["mode"] == "semantic"

        assert client.post("/api/admin/fallback", json={"on": "maybe"}, headers=AUTH).status_code == 422
        conn = db_conn(client)
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'fallback%'")]
        conn.close()
    assert actions == ["fallback_on", "fallback_off"]


# --- keyword mode -------------------------------------------------------------------


def test_keyword_search_with_snippet_and_bm25_order(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        ids = load_corpus(client)
        body = search(client, "glacier lifecycle")
    assert body["mode"] == "keyword"
    top = body["results"][0]
    assert top["doc_id"] == ids["s3"] and top["heading_path"].endswith("Lifecycle rules")
    assert "<b>" in top["snippet"]
    assert top["score"] > 0


@pytest.mark.parametrize(
    "q", ['"', "-", "*", "AND", "OR", "NOT", "NEAR(", "a-b", "volume AND", '"unclosed', "(((", "col:on", "^x", "'", "\\", "%", "a\"b"]
)
def test_fts_hostile_input_does_not_error(open_app: OpenApp, q: str) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        resp = client.get("/api/search", params={"q": q})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "keyword"


def test_hostile_input_also_safe_on_the_semantic_path(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        for q in ['"', "-", "*", "AND", "volume AND NOT"]:
            assert client.get("/api/search", params={"q": q}).status_code == 200


def test_keyword_confidence_is_never_high_and_medium_needs_every_term(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        all_terms = search(client, "attach EBS volume Linux instance")
        assert all_terms["confidence"] == "medium"
        # Stemming: "volumes" matches "volume" because the check uses FTS5's tokenizer.
        assert search(client, "snapshots volumes")["confidence"] == "medium"
        partial = search(client, "attach volume zebra")
        assert partial["results"] and partial["confidence"] == "low"
        none = search(client, "zebra")
        assert none["results"] == [] and none["confidence"] == "low"
        for q in ("attach EBS volume", "glacier", "health check target", "route table subnet"):
            assert search(client, q)["confidence"] in {"medium", "low"}
        assert len(gap_rows(client)) == 2  # the partial match and the empty result


def test_keyword_gap_normalization(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        search(client, "How do I configure DNS failover?")
        search(client, "configure dns failover")
        rows = gap_rows(client)
    assert [r["query_norm"] for r in rows] == ["configure dns failover"] * 2


# --- tags -----------------------------------------------------------------------------


@pytest.mark.parametrize("models", [True, False])
def test_tags_filter_requires_all_tags(open_app: OpenApp, models: bool) -> None:
    with open_app(models=models) as client:
        ids = load_corpus(client)
        q = "volume subnet storage network instance"
        unfiltered = {r["doc_id"] for r in search(client, q, k=20)["results"]}
        networking = {r["doc_id"] for r in search(client, q, tags="networking", k=20)["results"]}
        both = {r["doc_id"] for r in search(client, q, tags="networking,security", k=20)["results"]}
        storage = {r["doc_id"] for r in search(client, q, tags="storage", k=20)["results"]}
        nothing = search(client, q, tags="networking,storage")
        unknown = search(client, q, tags="nope")
    assert {ids["ebs"], ids["vpc"]} <= unfiltered
    assert networking == {ids["elb"], ids["vpc"]} or networking <= {ids["elb"], ids["vpc"]}
    assert networking and ids["ebs"] not in networking
    assert both == {ids["vpc"]}
    assert storage and storage <= {ids["ebs"], ids["s3"]}
    assert nothing["results"] == [] and nothing["confidence"] == "low"
    assert unknown["results"] == []


def test_tags_filter_applies_to_fts_union(open_app: OpenApp) -> None:
    """An exact-identifier chunk from a doc outside the tag filter must not leak in."""
    with open_app() as client:
        load_corpus(client)
        body = search(client, "InsufficientInstanceCapacity", tags="networking")
    assert all("Launch failures" not in r["heading_path"] for r in body["results"])


# --- write path hooks -------------------------------------------------------------------


def test_upload_embeds_chunks_and_updates_the_index(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        assert client.get("/healthz").json()["index_size"] == 0
        md, tags = DOCS["ebs"]
        doc_id = upload(client, md, tags)
        chunks = client.get(f"/api/docs/{doc_id}", params={"include_chunks": "true"}).json()["chunks"]
        health = client.get("/healthz").json()
        conn = db_conn(client)
        rows = conn.execute("SELECT id, embedding, embed_model, text, heading_path FROM chunks").fetchall()
        conn.close()
    assert health["index_size"] == health["chunks"] == len(chunks)
    assert all(r["embed_model"] == "fake-bow-384" and len(r["embedding"]) == 384 * 4 for r in rows)
    stored = np.frombuffer(rows[0]["embedding"], dtype=np.float32)
    expected = embedder.embed_passages([passage_text("Amazon EBS volumes", rows[0]["heading_path"], rows[0]["text"])])[0]
    np.testing.assert_allclose(stored, expected, atol=1e-6)
    assert embedder.passage_calls[0][0].startswith("Amazon EBS volumes")


def test_embedding_failure_after_upload_is_logged_and_left_for_backfill(
    open_app: OpenApp, caplog: pytest.LogCaptureFixture
) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        embedder.fail = True
        md, tags = DOCS["s3"]
        with caplog.at_level(logging.ERROR):
            resp = client.post("/api/docs", json={"content": md}, headers=AUTH)
        assert resp.status_code == 201  # the document is stored regardless
        doc_id = resp.json()["doc_id"]
        assert client.get("/healthz").json()["index_size"] == 0
        conn = db_conn(client)
        assert conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 3
        conn.close()
    assert any("embedding before store failed" in r.getMessage() for r in caplog.records)
    # The embedder was tried once (before the write), not again after it.
    assert len(embedder.passage_calls) == 1
    # The next startup backfills it.
    fresh = FakeEmbedder()
    with open_app(fresh) as client:
        assert client.get("/healthz").json()["index_size"] == 3
        assert fresh.passages_embedded == 3
        assert search(client, QUERIES["s3_lifecycle"][0])["results"][0]["doc_id"] == doc_id


def test_upload_while_embedder_unhealthy_leaves_null_embeddings(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        embedder.healthy = False
        md, _ = DOCS["elb"]
        upload(client, md)
        assert embedder.passage_calls == []
        conn = db_conn(client)
        assert conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 3
        conn.close()
        embedder.healthy = True
        body = search(client, QUERIES["elb_health"][0])
    # Not in the vector index yet, but the FTS union still lets the reranker see it.
    assert body["mode"] == "semantic"
    assert body["results"][0]["heading_path"].endswith("Health checks")


def test_delete_removes_the_doc_from_the_index_and_results(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        q = QUERIES["s3_lifecycle"][0]
        assert search(client, q)["results"][0]["doc_id"] == ids["s3"]
        before = client.get("/healthz").json()["index_size"]
        assert client.delete(f"/api/docs/{ids['s3']}", headers=AUTH).status_code == 200
        body = search(client, q)
        after = client.get("/healthz").json()["index_size"]
        keyword = search(client, "glacier lifecycle", **{})
    assert all(r["doc_id"] != ids["s3"] for r in body["results"] + keyword["results"])
    assert before - after == 3
    index = client.app.state.search.index
    assert ids["s3"] not in index.doc_ids()


def test_hooks_log_instead_of_raising(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with open_app() as client:
        state = client.app.state.search

        def boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("index exploded")

        md, tags = DOCS["vpc"]
        doc_id = upload(client, md, tags)
        monkeypatch.setattr(state.index, "remove_doc", boom)
        monkeypatch.setattr(state.index, "replace_doc", boom)
        with caplog.at_level(logging.ERROR):
            assert client.delete(f"/api/docs/{doc_id}", headers=AUTH).status_code == 200
            md2, _ = DOCS["elb"]
            assert client.post("/api/docs", json={"content": md2}, headers=AUTH).status_code == 201
    messages = [r.getMessage() for r in caplog.records]
    assert any("removing vectors after delete failed" in m for m in messages)
    assert any("publishing vectors to the index failed" in m for m in messages)


# --- startup: backfill and rebuild -----------------------------------------------------


def test_startup_backfills_null_and_stale_embeddings(open_app: OpenApp) -> None:
    with open_app(models=False) as client:  # uploads made while the models were down
        ids = load_corpus(client)
        conn = db_conn(client)
        total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == total
        conn.close()
        assert client.get("/healthz").json()["models"] == {"embedder": "off", "reranker": "off"}

    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        health = client.get("/healthz").json()
        body = search(client, QUERIES["nat"][0])
        conn = db_conn(client)
        null_left = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        models = {r[0] for r in conn.execute("SELECT DISTINCT embed_model FROM chunks")}
        # Make one chunk look like it came from an older model.
        conn.execute("UPDATE chunks SET embed_model = 'old-model' WHERE id = 1")
        conn.close()
    assert embedder.passages_embedded == total
    assert null_left == 0 and models == {"fake-bow-384"}
    assert health["index_size"] == total and health["mode"] == "semantic"
    assert body["mode"] == "semantic" and body["results"][0]["doc_id"] == ids["vpc"]

    again = FakeEmbedder()
    with open_app(again) as client:
        assert client.get("/healthz").json()["index_size"] == total
    assert again.passages_embedded == 1  # only the chunk from the "old" model


def test_backfill_works_in_batches_of_64(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        for n in range(70):
            upload(client, f"# Doc {n}\n\n" + f"batch filler text number{n} " * 20)
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        assert client.get("/healthz").json()["index_size"] == 70
    assert [len(batch) for batch in embedder.passage_calls] == [64, 6]


def test_restart_rebuilds_the_index_without_embedding(open_app: OpenApp) -> None:
    first = FakeEmbedder()
    with open_app(first) as client:
        ids = load_corpus(client)
        size = client.get("/healthz").json()["index_size"]
        expected = search(client, QUERIES["elb_health"][0])
    assert first.passages_embedded == size

    second = FakeEmbedder()  # a replacement instance: new process, same database
    with open_app(second) as client:
        assert client.get("/healthz").json()["index_size"] == size
        again = search(client, QUERIES["elb_health"][0])
    assert second.passage_calls == [], "restart must not re-embed any chunk"
    assert [r["chunk_id"] for r in again["results"]] == [r["chunk_id"] for r in expected["results"]]
    assert again["results"][0]["doc_id"] == ids["elb"]


def test_startup_survives_an_embedder_that_dies_during_backfill(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
    with open_app(FakeEmbedder(fail=True)) as client:
        health = client.get("/healthz")
        body = search(client, "attach EBS volume Linux")
    assert health.status_code == 200
    assert body["results"]  # keyword or semantic via FTS, but it answers


# --- REST validation and metrics -------------------------------------------------------------


def test_rest_validation(open_app: OpenApp) -> None:
    with open_app() as client:
        for params in ({}, {"q": ""}, {"q": "   "}, {"q": "x", "k": 0}, {"q": "x", "k": "many"}):
            resp = client.get("/api/search", params=params)
            assert resp.status_code == 422, params
            assert resp.json()["error"] == "invalid_request"


def test_search_needs_no_token(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.get("/api/search", params={"q": "anything"}).status_code == 200


def test_search_emits_one_metrics_line(open_app: OpenApp, caplog: pytest.LogCaptureFixture) -> None:
    with open_app() as client:
        load_corpus(client)
        with caplog.at_level(logging.INFO):
            search(client, QUERIES["ebs_linux"][0])
    metric_records = [r for r in caplog.records if hasattr(r, "_aws")]
    assert len(metric_records) == 1
    rec = metric_records[0]
    assert rec.Mode == "semantic" and rec.KeywordSearches == 0 and rec.LatencyMs > 0
    assert rec.ConfidenceLevel in (0, 1, 2)


def test_keyword_search_metrics_flag(open_app: OpenApp, caplog: pytest.LogCaptureFixture) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        with caplog.at_level(logging.INFO):
            search(client, "glacier lifecycle")
    rec = [r for r in caplog.records if hasattr(r, "_aws")][0]
    assert (rec.Mode, rec.KeywordSearches) == ("keyword", 1)


# --- KB_RERANK=off ------------------------------------------------------------------------------


def test_rerank_off_ranks_by_cosine_with_cosine_cutoffs(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder, KB_RERANK="off") as client:
        load_corpus(client)
        health = client.get("/healthz").json()
        q = QUERIES["nat"][0]
        body = search(client, q)
        qvec = embedder.embed_query(q)
        conn = db_conn(client)
        top_chunk = conn.execute("SELECT embedding FROM chunks WHERE id = ?", (body["results"][0]["chunk_id"],)).fetchone()
        conn.close()
    assert health["mode"] == "semantic"
    assert health["models"] == {"embedder": "ok", "reranker": "off"}
    assert body["mode"] == "semantic"
    assert body["results"][0]["heading_path"].endswith("NAT gateway")
    cosine = float(np.frombuffer(top_chunk["embedding"], dtype=np.float32) @ qvec)
    assert body["results"][0]["score"] == pytest.approx(cosine, abs=1e-4)
    scores = [r["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("high, low, expected", [("0.1", "0.05", "high"), ("0.99", "0.05", "medium"), ("0.99", "0.98", "low")])
def test_rerank_off_confidence_uses_cosine_cutoffs(open_app: OpenApp, high: str, low: str, expected: str) -> None:
    env = {"KB_RERANK": "off", "KB_COSINE_CONF_HIGH": high, "KB_COSINE_CONF_LOW": low}
    with open_app(**env) as client:
        load_corpus(client)
        assert search(client, QUERIES["ebs_linux"][0])["confidence"] == expected


def test_rerank_off_never_loads_or_calls_a_reranker(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_RERANK="off") as client:
        load_corpus(client)
        search(client, QUERIES["nat"][0])
        assert client.app.state.search.reranker is None
    assert reranker.score_calls == []


def test_rerank_off_setting_parses() -> None:
    assert Settings.from_env({}).rerank_on is True
    assert Settings.from_env({"KB_RERANK": "OFF"}).rerank_on is False
    s = Settings.from_env({})
    assert (s.cosine_conf_high, s.cosine_conf_low) == (0.75, 0.55)


# --- test-only fake models via the environment --------------------------------------------------------


def test_kb_fake_models_env_swaps_in_the_doubles(
    db_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kb import config
    from kb.main import create_app

    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "on")
    monkeypatch.setenv("KB_FAKE_MODELS", "1")
    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken")
    config.clear_token_cache()
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["models"] == {"embedder": "ok", "reranker": "ok"}
        load_corpus(client)
        assert search(client, QUERIES["nat"][0])["mode"] == "semantic"
    config.clear_token_cache()


def test_fake_models_env_is_ignored_when_models_are_off(
    db_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kb import config
    from kb.main import create_app

    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "off")
    monkeypatch.setenv("KB_FAKE_MODELS", "1")
    config.clear_token_cache()
    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["mode"] == "keyword"


# --- rerank pool size and passage cut (phase 11: t3.medium timing) ----------------------


def test_rerank_pool_is_capped_by_kb_rerank_candidates(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_RERANK_CANDIDATES="2") as client:
        load_corpus(client)
        search(client, "zzzz qqqq")  # no FTS matches, so only the cosine pool is reranked
    assert len(reranker.score_calls[0][1]) == 2


def test_rerank_pool_adds_at_most_five_fts_hits(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_RERANK_CANDIDATES="1") as client:
        load_corpus(client)
        search(client, "volume instance bucket subnet load balancer")
    assert 1 < len(reranker.score_calls[0][1]) <= 1 + pipeline.FTS_CANDIDATES == 6


def test_reranked_passages_are_cut_to_kb_rerank_max_chars(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_RERANK_MAX_CHARS="120") as client:
        load_corpus(client)
        body = search(client, QUERIES["nat"][0])
    assert body["mode"] == "semantic"
    texts = reranker.score_calls[0][1]
    assert texts and max(len(t) for t in texts) == 120
    # The hit itself still carries the whole chunk's snippet, not the cut text.
    assert body["results"][0]["snippet"]


def test_without_the_reranker_the_cosine_pool_stays_30(open_app: OpenApp, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    with open_app(KB_RERANK="off", KB_RERANK_CANDIDATES="2") as client:
        index = client.app.state.search.index
        original = index.top_k

        def spy(qvec, k, allowed=None):  # type: ignore[no-untyped-def]
            seen.append(k)
            return original(qvec, k, allowed)

        monkeypatch.setattr(index, "top_k", spy)
        load_corpus(client)
        search(client, QUERIES["nat"][0])
    assert seen == [pipeline.SEMANTIC_CANDIDATES]
