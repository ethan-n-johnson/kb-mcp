"""VectorIndex: load, add, remove, top-k, snapshots."""

from __future__ import annotations

import sqlite3
import threading

import numpy as np

from kb.search.index import DIM, VectorIndex
from tests.fakes import FakeEmbedder

E = FakeEmbedder()


def vecs(*texts: str) -> np.ndarray:
    return E.embed_passages(list(texts))


def make_index() -> VectorIndex:
    idx = VectorIndex()
    idx.add(
        [10, 11, 12, 13],
        [1, 1, 2, 3],
        vecs("attach ebs volume", "ebs snapshot copy", "s3 lifecycle glacier", "nat gateway subnet"),
    )
    return idx


def test_top_k_orders_by_cosine() -> None:
    idx = make_index()
    hits = idx.top_k(E.embed_query("attach an ebs volume"), 3)
    assert [h[0] for h in hits][0] == 10
    assert [h[2] for h in hits] == sorted([h[2] for h in hits], reverse=True)
    assert len(hits) == 3
    assert hits[0][1] == 1  # doc id
    assert isinstance(hits[0][0], int) and isinstance(hits[0][2], float)


def test_top_k_larger_than_index_and_empty_index() -> None:
    idx = make_index()
    assert len(idx.top_k(E.embed_query("x"), 100)) == 4
    assert VectorIndex().top_k(E.embed_query("x"), 5) == []
    assert idx.top_k(E.embed_query("x"), 0) == []


def test_top_k_allowed_doc_ids_masks() -> None:
    idx = make_index()
    q = E.embed_query("attach ebs volume")
    assert {h[1] for h in idx.top_k(q, 10, allowed_doc_ids=[2, 3])} == {2, 3}
    assert idx.top_k(q, 10, allowed_doc_ids=[]) == []
    assert idx.top_k(q, 10, allowed_doc_ids=[99]) == []
    assert len(idx.top_k(q, 10, allowed_doc_ids=[1])) == 2


def test_remove_doc_and_len() -> None:
    idx = make_index()
    assert len(idx) == 4
    assert idx.remove_doc(1) == 2
    assert len(idx) == 2
    assert idx.remove_doc(1) == 0
    assert all(h[1] != 1 for h in idx.top_k(E.embed_query("ebs"), 10))


def test_add_replaces_existing_chunk_id() -> None:
    idx = make_index()
    idx.add([10], [1], vecs("completely different words"))
    assert len(idx) == 4
    assert idx.top_k(E.embed_query("completely different words"), 1)[0][0] == 10


def test_add_validates_shapes() -> None:
    idx = VectorIndex()
    for bad in (
        lambda: idx.add([1], [1], np.zeros((1, 10), dtype=np.float32)),
        lambda: idx.add([1, 2], [1], vecs("a")),
    ):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    idx.add([], [], np.zeros((0, DIM), dtype=np.float32))
    assert len(idx) == 0


def test_doc_vector_is_normalized_mean() -> None:
    idx = make_index()
    v = idx.doc_vector(1)
    assert v is not None and abs(float(np.linalg.norm(v)) - 1.0) < 1e-5
    expected = vecs("attach ebs volume", "ebs snapshot copy").mean(axis=0)
    expected /= np.linalg.norm(expected)
    np.testing.assert_allclose(v, expected, atol=1e-5)
    assert idx.doc_vector(99) is None


def test_load_from_db_reads_blobs_for_matching_model(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO docs(id, title, source_type, content_md, content_hash, created_at, updated_at)"
        " VALUES (1, 'T', 'markdown', 'b', 'h', 'n', 'n')"
    )
    v = vecs("one", "two", "three", "four")
    rows = [
        (1, "m1", v[0].tobytes()),
        (2, "m1", v[1].tobytes()),
        (3, "other-model", v[2].tobytes()),
        (4, None, None),
        (5, "m1", b"short"),
    ]
    for chunk_id, model, blob in rows:
        conn.execute(
            "INSERT INTO chunks(id, doc_id, ord, heading_path, text, embedding, embed_model)"
            " VALUES (?, 1, ?, 'h', 't', ?, ?)",
            (chunk_id, chunk_id, blob, model),
        )
    idx = VectorIndex()
    assert idx.load_from_db(conn, "m1") == 2
    assert len(idx) == 2
    top = idx.top_k(v[1], 1)[0]
    assert top[0] == 2 and top[2] > 0.99
    assert idx.load_from_db(conn, "missing") == 0 and len(idx) == 0


def test_readers_see_consistent_snapshots_under_concurrent_writes() -> None:
    idx = VectorIndex()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            idx.add([n], [n % 7], vecs(f"word{n}"))
            if n % 5 == 0:
                idx.remove_doc(n % 7)

    def reader() -> None:
        q = E.embed_query("word1")
        try:
            while not stop.is_set():
                for chunk_id, doc_id, score in idx.top_k(q, 5):
                    assert doc_id == chunk_id % 7 and -1.01 <= score <= 1.01
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), *[threading.Thread(target=reader) for _ in range(3)]]
    for t in threads:
        t.start()
    threading.Event().wait(0.6)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []


def test_replace_doc_swaps_all_of_a_docs_vectors() -> None:
    idx = make_index()  # doc 1 has chunks 10 and 11
    idx.replace_doc(1, [20, 21, 22], vecs("new text one", "new text two", "new text three"))
    assert len(idx) == 5
    assert {h[0] for h in idx.top_k(E.embed_query("x"), 10) if h[1] == 1} == {20, 21, 22}
    assert idx.top_k(E.embed_query("new text two"), 1)[0][0] == 21
    assert idx.doc_ids() == {1, 2, 3}


def test_replace_doc_with_nothing_removes_the_doc_and_adds_a_new_one() -> None:
    idx = make_index()
    idx.replace_doc(1, [], np.zeros((0, DIM), dtype=np.float32))
    assert len(idx) == 2 and idx.doc_ids() == {2, 3}
    idx.replace_doc(9, [30], vecs("brand new doc"))
    assert idx.doc_ids() == {2, 3, 9}


def test_replace_doc_validates_lengths() -> None:
    idx = make_index()
    try:
        idx.replace_doc(1, [20, 21], vecs("only one"))
    except ValueError:
        assert len(idx) == 4  # unchanged
        return
    raise AssertionError("expected ValueError")


def test_doc_vectors_match_doc_vector_for_every_doc() -> None:
    idx = make_index()
    doc_ids, matrix = idx.doc_vectors()
    assert sorted(doc_ids.tolist()) == [1, 2, 3] and matrix.shape == (3, DIM)
    assert matrix.dtype == np.float32
    for row, doc_id in zip(matrix, doc_ids.tolist()):
        np.testing.assert_allclose(row, idx.doc_vector(doc_id), atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)


def test_doc_vectors_of_an_empty_index_and_cancelling_vectors() -> None:
    ids, matrix = VectorIndex().doc_vectors()
    assert ids.shape == (0,) and matrix.shape == (0, DIM)
    idx = VectorIndex()
    v = vecs("something")[0]
    idx.add([1, 2], [5, 5], np.stack([v, -v]))  # the doc's mean is the zero vector
    ids, matrix = idx.doc_vectors()
    assert ids.shape == (0,) and matrix.shape == (0, DIM)
