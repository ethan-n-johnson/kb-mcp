"""Embedder and Reranker wrappers, driven by stub fastembed models."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pytest

from kb.search.embedder import DIM, QUERY_PREFIX, Embedder, passage_text
from kb.search.health import HealthTracker
from kb.search.reranker import Reranker, sigmoid
from tests.fakes import FakeEmbedder


class StubTextEmbedding:
    """Mimics the parts of `fastembed.TextEmbedding` that `Embedder` uses.

    Like the real class for bge-small, `query_embed` adds no prefix of its own.
    It returns un-normalized vectors so the wrapper's normalization is tested.
    """

    def __init__(self, name: str, cache_dir: str | None = None) -> None:
        self.name, self.cache_dir = name, cache_dir
        self.passages: list[str] = []
        self.queries: list[str] = []
        self.fail = False
        self._fake = FakeEmbedder()

    def _vectors(self, texts: Iterable[str]) -> list[np.ndarray]:
        if self.fail:
            raise RuntimeError("model blew up")
        return [v * 3.0 for v in self._fake.embed(list(texts))]

    def passage_embed(self, texts: Iterable[str], **kwargs: Any) -> Iterable[np.ndarray]:
        items = list(texts)
        self.passages.extend(items)
        yield from self._vectors(items)

    def query_embed(self, query: str | Iterable[str], **kwargs: Any) -> Iterable[np.ndarray]:
        items = [query] if isinstance(query, str) else list(query)
        self.queries.extend(items)
        yield from self._vectors(items)


def make_embedder(**kw: Any) -> tuple[Embedder, StubTextEmbedding]:
    holder: list[StubTextEmbedding] = []

    def factory(name: str, cache_dir: str | None = None) -> StubTextEmbedding:
        holder.append(StubTextEmbedding(name, cache_dir))
        return holder[0]

    emb = Embedder("/cache", model_factory=factory, **kw)
    assert emb.load()
    return emb, holder[0]


# --- query prefix ---------------------------------------------------------------


def test_query_prefix_is_applied_exactly_once() -> None:
    emb, model = make_embedder()
    emb.embed_query("attach an ebs volume")
    assert model.queries == [QUERY_PREFIX + "attach an ebs volume"]
    assert model.queries[0].count(QUERY_PREFIX) == 1
    assert QUERY_PREFIX == "Represent this sentence for searching relevant passages: "


def test_passages_get_no_prefix() -> None:
    emb, model = make_embedder()
    emb.embed_passages(["one", "two"])
    assert model.passages == ["one", "two"]


def test_fastembed_adds_no_query_prefix_for_bge_small() -> None:
    """Pins the finding that justifies adding the prefix ourselves.

    fastembed 0.9.0 serves bge-small-en-v1.5 with `OnnxTextEmbedding`, which does
    not override `query_embed`; the base implementation just calls `embed`. If a
    fastembed upgrade starts adding its own prefix, this fails and
    `Embedder.embed_query` would otherwise double it.
    """
    from fastembed.text.onnx_embedding import OnnxTextEmbedding
    from fastembed.text.text_embedding_base import TextEmbeddingBase

    assert OnnxTextEmbedding.query_embed is TextEmbeddingBase.query_embed


def test_loader_passes_model_name_and_cache_dir() -> None:
    emb, model = make_embedder()
    assert (model.name, model.cache_dir) == ("BAAI/bge-small-en-v1.5", "/cache")
    assert emb.model_name == "BAAI/bge-small-en-v1.5"


# --- shapes and normalization ---------------------------------------------------


def test_outputs_are_float32_unit_norm() -> None:
    emb, _ = make_embedder()
    passages = emb.embed_passages(["a b c", "d e f", "g"])
    query = emb.embed_query("a b")
    assert passages.shape == (3, DIM) and passages.dtype == np.float32
    assert query.shape == (DIM,) and query.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(passages, axis=1), 1.0, atol=1e-5)
    assert abs(float(np.linalg.norm(query)) - 1.0) < 1e-5


def test_empty_passages_need_no_model_call() -> None:
    emb, model = make_embedder()
    out = emb.embed_passages([])
    assert out.shape == (0, DIM)
    assert model.passages == []


def test_wrong_dimension_is_an_error_and_counts_as_failure() -> None:
    emb, model = make_embedder()
    model._vectors = lambda texts: [np.ones(10, dtype=np.float32) for _ in texts]  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        emb.embed_query("x")
    assert emb._health.consecutive_failures == 1


# --- health ---------------------------------------------------------------------


def test_load_failure_is_logged_not_raised() -> None:
    def boom(name: str, cache_dir: str | None = None) -> Any:
        raise OSError("no network")

    emb = Embedder("/cache", model_factory=boom)
    assert emb.load() is False
    assert emb.healthy is False and emb.status == "failed"
    with pytest.raises(RuntimeError):
        emb.embed_query("x")


def test_unloaded_embedder_reports_off() -> None:
    emb = Embedder("/cache", model_factory=StubTextEmbedding)
    assert (emb.healthy, emb.status) == (False, "off")


def test_three_consecutive_failures_mark_unhealthy_until_next_success() -> None:
    now = [0.0]
    emb, model = make_embedder(health=HealthTracker(probe_interval=30.0, clock=lambda: now[0]))
    model.fail = True
    for _ in range(2):
        with pytest.raises(RuntimeError):
            emb.embed_query("x")
        assert emb.healthy
    with pytest.raises(RuntimeError):
        emb.embed_query("x")
    assert not emb.healthy and emb.status == "failed"
    # Still unhealthy inside the probe interval.
    now[0] = 29.0
    assert not emb.healthy
    # After the interval one probe is allowed; it fails and re-opens the window.
    now[0] = 31.0
    assert emb.healthy
    with pytest.raises(RuntimeError):
        emb.embed_query("x")
    assert not emb.healthy
    # A later probe succeeds and clears the count.
    now[0] = 70.0
    model.fail = False
    assert emb.healthy
    emb.embed_query("x")
    assert emb.healthy and emb._health.consecutive_failures == 0


def test_a_success_resets_the_failure_streak() -> None:
    emb, model = make_embedder()
    for _ in range(2):
        model.fail = True
        with pytest.raises(RuntimeError):
            emb.embed_query("x")
    model.fail = False
    emb.embed_query("x")
    model.fail = True
    for _ in range(2):
        with pytest.raises(RuntimeError):
            emb.embed_query("x")
    assert emb.healthy  # 2 in a row since the success, not 3


# --- passage text ---------------------------------------------------------------


def test_passage_text_does_not_repeat_the_title() -> None:
    assert passage_text("EBS", "EBS > Attach", "body") == "EBS > Attach\n\nbody"
    assert passage_text("EBS", "EBS", "body") == "EBS\n\nbody"
    assert passage_text("EBS", "Attach > Linux", "body") == "EBS > Attach > Linux\n\nbody"
    assert passage_text("EBS", "", "body") == "EBS\n\nbody"


# --- reranker -------------------------------------------------------------------


class StubCrossEncoder:
    def __init__(self, name: str, cache_dir: str | None = None) -> None:
        self.name, self.cache_dir = name, cache_dir
        self.raw = [8.0, -8.0, 0.0]
        self.fail = False

    def rerank(self, query: str, documents: Iterable[str], **kwargs: Any) -> Iterable[float]:
        if self.fail:
            raise RuntimeError("nope")
        docs = list(documents)
        yield from self.raw[: len(docs)]


def make_reranker() -> tuple[Reranker, StubCrossEncoder]:
    holder: list[StubCrossEncoder] = []

    def factory(name: str, cache_dir: str | None = None) -> StubCrossEncoder:
        holder.append(StubCrossEncoder(name, cache_dir))
        return holder[0]

    rr = Reranker("/cache", model_factory=factory)
    assert rr.load()
    return rr, holder[0]


def test_reranker_returns_sigmoid_of_raw_scores() -> None:
    rr, model = make_reranker()
    out = rr.score("q", ["a", "b", "c"])
    assert out.dtype == np.float32 and out.shape == (3,)
    np.testing.assert_allclose(out, [1 / (1 + np.exp(-8)), 1 / (1 + np.exp(8)), 0.5], rtol=1e-5)
    assert model.name == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert rr.score("q", []).shape == (0,)


def test_sigmoid_is_stable_for_extreme_values() -> None:
    out = sigmoid(np.array([-1000.0, 1000.0]))
    assert out.tolist() == [0.0, 1.0]


def test_reranker_health_pattern() -> None:
    rr, model = make_reranker()
    model.fail = True
    for _ in range(3):
        with pytest.raises(RuntimeError):
            rr.score("q", ["a"])
    assert not rr.healthy and rr.status == "failed"

    def boom(name: str, cache_dir: str | None = None) -> Any:
        raise OSError("x")

    bad = Reranker("/c", model_factory=boom)
    assert bad.load() is False and bad.status == "failed"
