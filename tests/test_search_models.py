"""Search with the REAL bge-small embedder and cross-encoder (`pytest -m models`).

These cannot run in the cloud workspace (the ONNX models need HuggingFace) and
are run on the laptop. The first run downloads the models into `KB_MODEL_DIR`
(default `.localdata/models`). Each scenario mirrors a fast test in
`tests/test_search.py` that runs the same code path with the fakes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
import pytest

from kb.search.embedder import MODEL_NAME, QUERY_PREFIX, Embedder
from kb.search.reranker import Reranker
from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS, QUERIES
from tests.helpers import db_conn, gap_rows, load_corpus, search, upload

pytestmark = pytest.mark.models

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def model_dir() -> str:
    return os.environ.get("KB_MODEL_DIR") or str(ROOT / ".localdata" / "models")


@pytest.fixture(scope="module")
def real_embedder(model_dir: str) -> Embedder:
    embedder = Embedder(model_dir)
    if not embedder.load():
        pytest.fail("the embedding model failed to load (see the log); is KB_MODEL_DIR right?")
    return embedder


@pytest.fixture(scope="module")
def real_reranker(model_dir: str) -> Reranker:
    reranker = Reranker(model_dir)
    if not reranker.load():
        pytest.fail("the reranker model failed to load (see the log); is KB_MODEL_DIR right?")
    return reranker


class Preloaded:
    """Wraps an already loaded real model: counts and optionally delays calls.

    `load()` does not reload (loading takes seconds), everything else is
    delegated, so the app lifespan treats it like a freshly loaded model.
    """

    def __init__(self, inner: Any, delay: float = 0.0) -> None:
        self.inner = inner
        self.delay = delay
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def load(self) -> bool:
        return bool(self.inner.healthy)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def embed_passages(self, texts: Sequence[str], *a: Any, **k: Any) -> np.ndarray:
        self.passage_calls.append(list(texts))
        return self.inner.embed_passages(texts, *a, **k)

    def embed_query(self, query: str) -> np.ndarray:
        self.query_calls.append(query)
        if self.delay:
            time.sleep(self.delay)
        return self.inner.embed_query(query)

    @property
    def passages_embedded(self) -> int:
        return sum(len(b) for b in self.passage_calls)


@pytest.fixture
def wrap(real_embedder: Embedder, real_reranker: Reranker) -> Iterator[Any]:
    def make(delay: float = 0.0) -> tuple[Preloaded, Preloaded]:
        return Preloaded(real_embedder, delay), Preloaded(real_reranker)

    yield make


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


# --- the models themselves -------------------------------------------------------------


def test_embedder_shape_norm_and_relevance(real_embedder: Embedder) -> None:
    passages = real_embedder.embed_passages(
        [
            "To attach an EBS volume, choose Actions, Attach volume, then pick the instance.",
            "Amazon S3 lifecycle rules move objects to Glacier after a number of days.",
        ]
    )
    query = real_embedder.embed_query("How do I attach an EBS volume to an EC2 instance?")
    assert passages.shape == (2, 384) and passages.dtype == np.float32
    assert query.shape == (384,) and query.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(passages, axis=1), 1.0, atol=1e-4)
    assert float(query @ passages[0]) > float(query @ passages[1])


def test_query_prefix_is_applied_exactly_once_with_the_real_model(real_embedder: Embedder) -> None:
    """Pins fastembed's behavior: no built-in prefix, so ours must be added exactly once."""
    raw: Any = real_embedder._model  # the underlying fastembed.TextEmbedding
    q = "how do I attach an ebs volume"

    def embed(text: str) -> np.ndarray:
        v = np.asarray(next(iter(raw.embed([text]))), dtype=np.float32)
        return v / np.linalg.norm(v)

    plain = embed(q)
    via_query_embed = np.asarray(next(iter(raw.query_embed(q))), dtype=np.float32)
    via_query_embed = via_query_embed / np.linalg.norm(via_query_embed)
    prefixed = embed(QUERY_PREFIX + q)
    doubled = embed(QUERY_PREFIX + QUERY_PREFIX + q)
    ours = real_embedder.embed_query(q)

    # fastembed's query_embed adds no prefix for this model ...
    np.testing.assert_allclose(via_query_embed, plain, atol=1e-4)
    # ... so embed_query adds it exactly once: equal to one prefix, not none, not two.
    np.testing.assert_allclose(ours, prefixed, atol=1e-4)
    assert not np.allclose(ours, plain, atol=1e-3), "prefix missing"
    assert not np.allclose(ours, doubled, atol=1e-3), "prefix doubled"
    assert real_embedder.model_name == MODEL_NAME


def test_reranker_scores_are_sigmoids_and_ordered(real_reranker: Reranker) -> None:
    query = "How do I attach an EBS volume to an EC2 instance?"
    scores = real_reranker.score(
        query,
        [
            "To attach an EBS volume, choose Actions, Attach volume, and select the instance.",
            "Amazon S3 lifecycle rules move objects to Glacier after a number of days.",
        ],
    )
    assert scores.shape == (2,)
    assert 0.0 < float(scores[1]) < float(scores[0]) < 1.0
    assert float(scores[0]) > 0.7 and float(scores[1]) < 0.3


# --- the pipeline with real models ---------------------------------------------------------


@pytest.mark.parametrize("name", list(QUERIES))
def test_semantic_query_finds_the_right_chunk_at_rank_one(
    open_app: OpenApp, wrap: Any, name: str
) -> None:
    query, doc_key, path_suffix = QUERIES[name]
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        ids = load_corpus(client)
        body = search(client, query)
    assert body["mode"] == "semantic"
    top = body["results"][0]
    assert top["doc_id"] == ids[doc_key] and top["heading_path"].endswith(path_suffix)
    assert body["confidence"] in {"high", "medium"}
    assert [r["score"] for r in body["results"]] == sorted((r["score"] for r in body["results"]), reverse=True)
    per_doc: dict[int, int] = {}
    for r in body["results"]:
        per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
    assert max(per_doc.values()) <= 2


def test_off_topic_query_is_low_confidence_and_logged_as_a_gap(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        load_corpus(client)
        body = search(client, "What is the airspeed velocity of an unladen swallow?")
        rows = gap_rows(client)
    assert body["mode"] == "semantic" and body["confidence"] == "low"
    assert len(rows) == 1 and rows[0]["mode"] == "semantic" and rows[0]["top_score"] is not None


def test_toggle_forces_keyword_mode(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        load_corpus(client)
        q = QUERIES["ebs_linux"][0]
        assert search(client, q)["mode"] == "semantic"
        client.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        assert search(client, q)["mode"] == "keyword"
        assert client.get("/healthz").json()["mode"] == "keyword"
        client.post("/api/admin/fallback", json={"on": False}, headers=AUTH)
        assert search(client, q)["mode"] == "semantic"
        health = client.get("/healthz").json()
    assert health["models"] == {"embedder": "ok", "reranker": "ok"}


def test_slow_embedder_falls_back_to_keyword(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr, KB_SEARCH_TIMEOUT="0.5") as client:
        load_corpus(client)
        emb.delay = 3.0  # monkeypatched slow embedder: sleeps past the timeout
        started = time.perf_counter()
        body = search(client, "attach EBS volume Linux")
        elapsed = time.perf_counter() - started
    assert body["mode"] == "keyword" and body["results"]
    assert elapsed < 2.5


def test_delete_removes_the_doc_from_results(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        ids = load_corpus(client)
        q = QUERIES["s3_lifecycle"][0]
        assert search(client, q)["results"][0]["doc_id"] == ids["s3"]
        assert client.delete(f"/api/docs/{ids['s3']}", headers=AUTH).status_code == 200
        after = search(client, q)
    assert all(r["doc_id"] != ids["s3"] for r in after["results"])


def test_restart_rebuilds_the_index_with_zero_embed_calls(open_app: OpenApp, wrap: Any) -> None:
    emb1, rr1 = wrap()
    with open_app(emb1, rr1) as client:
        load_corpus(client)
        size = client.get("/healthz").json()["index_size"]
        first = search(client, QUERIES["elb_health"][0])
    assert emb1.passages_embedded == size

    emb2, rr2 = wrap()  # new process, same database
    with open_app(emb2, rr2) as client:
        assert client.get("/healthz").json()["index_size"] == size
        second = search(client, QUERIES["elb_health"][0])
    assert emb2.passage_calls == [], "restart must not re-embed"
    assert [r["chunk_id"] for r in second["results"]] == [r["chunk_id"] for r in first["results"]]


def test_backfill_embeds_null_rows(open_app: OpenApp, wrap: Any) -> None:
    with open_app(models=False) as client:  # uploaded while the models were down
        ids = load_corpus(client)
        conn = db_conn(client)
        total = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        conn.close()
    assert total > 0
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        conn = db_conn(client)
        nulls = conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        conn.close()
        body = search(client, QUERIES["nat"][0])
        size = client.get("/healthz").json()["index_size"]
    assert nulls == 0 and emb.passages_embedded == total and size == total
    assert body["mode"] == "semantic" and body["results"][0]["doc_id"] == ids["vpc"]


def test_tags_filter_with_real_models(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        ids = load_corpus(client)
        body = search(client, "how are instances and storage volumes set up", tags="storage", k=10)
    assert body["results"] and {r["doc_id"] for r in body["results"]} <= {ids["ebs"], ids["s3"]}


def test_upload_embeds_with_the_real_model(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr) as client:
        md, tags = DOCS["vpc"]
        upload(client, md, tags)
        health = client.get("/healthz").json()
    assert health["index_size"] == health["chunks"] > 0
    assert emb.passages_embedded == health["chunks"]
