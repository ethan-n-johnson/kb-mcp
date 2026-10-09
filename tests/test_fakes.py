"""The fake embedder and reranker that later phases rely on."""

from __future__ import annotations

import numpy as np

from tests.fakes import DIM, FakeEmbedder, FakeReranker


def test_embedder_shape_dtype_norm() -> None:
    vecs = FakeEmbedder().embed_passages(["attach an EBS volume", "", "!!!", "route 53 dns"])
    assert vecs.shape == (4, 384) == (4, DIM)
    assert vecs.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)


def test_embedder_is_deterministic() -> None:
    a = FakeEmbedder().embed_query("same text")
    b = FakeEmbedder().embed_query("same text")
    np.testing.assert_array_equal(a, b)


def test_similar_texts_score_higher_than_unrelated() -> None:
    e = FakeEmbedder()
    q, close, far = e.embed_passages(
        [
            "how do I attach an EBS volume to an EC2 instance",
            "attach the EBS volume to your EC2 instance using the console",
            "chocolate cake recipe with butter and sugar",
        ]
    )
    assert float(q @ close) > float(q @ far)
    assert float(q @ close) > 0.4
    assert float(q @ far) < 0.3


def test_reranker_scores_by_overlap() -> None:
    r = FakeReranker()
    good, bad = r.score("attach ebs volume", ["how to attach an EBS volume", "cake recipe"])
    assert 0.0 < bad < good < 1.0
    assert good > 0.7 and bad < 0.3
