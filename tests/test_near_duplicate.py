"""Near-duplicate check on upload, with the fake embedder.

The fake embedder's similarities say nothing about bge's, so every test sets
`KB_DUP_THRESHOLD` explicitly (0.8: a lightly edited copy scores about 0.99,
unrelated corpus docs at most 0.53) instead of relying on the 0.92 default.
`tests/test_operations_models.py` repeats the scenario with the real model.
"""

from __future__ import annotations

import sqlite3
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kb import db
from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS, EBS, S3
from tests.fakes import FakeEmbedder
from tests.helpers import load_corpus, upload
from tests.ops_helpers import EBS_EDITED, ProbeEmbedder, audit_rows, post_doc, scalar

DUP = {"KB_DUP_THRESHOLD": 0.8}
SKIPPED = "skipped: models unavailable"


def test_lightly_edited_copy_is_a_possible_duplicate(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        before = (scalar(client, "SELECT count(*) FROM docs"), scalar(client, "SELECT count(*) FROM chunks"))
        resp = post_doc(client, EBS_EDITED)
        after = (scalar(client, "SELECT count(*) FROM docs"), scalar(client, "SELECT count(*) FROM chunks"))
    assert resp.status_code == 200  # a normal outcome, not an error
    body = resp.json()
    assert body["status"] == "possible_duplicate"
    top = body["candidates"][0]
    assert top["doc_id"] == ids["ebs"] and top["title"] == "Amazon EBS volumes"
    assert top["similarity"] >= 0.8
    assert set(top) == {"doc_id", "title", "similarity"}  # no source_url was stored
    assert "doc_id" not in body and "chunks" not in body
    assert "force=true" in body["message"]
    assert after == before  # nothing was written
    assert [a for _, a, _ in audit_rows(client)].count("upload") == len(ids)


def test_candidate_carries_the_source_url(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        doc_id = upload(client, EBS, ["storage"], source_url="https://docs.example/ebs")
        body = post_doc(client, EBS_EDITED).json()
    assert body["candidates"][0] == {
        "doc_id": doc_id,
        "title": "Amazon EBS volumes",
        "similarity": body["candidates"][0]["similarity"],
        "source_url": "https://docs.example/ebs",
    }


def test_force_saves_the_near_duplicate(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        first = upload(client, EBS)
        resp = post_doc(client, EBS_EDITED, force=True)
        total = scalar(client, "SELECT count(*) FROM docs")
    assert resp.status_code == 201
    assert resp.json()["doc_id"] != first and total == 2
    assert "near_duplicate_check" not in resp.json()  # the check could run; force only skipped the verdict


def test_unrelated_docs_are_not_near_duplicates(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)  # five different AWS docs: every upload returned 201
    assert len(set(ids.values())) == 5


def test_threshold_comes_from_the_setting(open_app: OpenApp) -> None:
    with open_app(KB_DUP_THRESHOLD=0.999999) as client:
        upload(client, EBS)
        # Similar, but below the cutoff, so it is stored.
        assert post_doc(client, EBS_EDITED).status_code == 201
    with open_app(KB_DUP_THRESHOLD=0.1) as client:
        # With a very low cutoff even an unrelated doc is flagged.
        assert post_doc(client, S3).json()["status"] == "possible_duplicate"


def test_at_most_three_candidates_best_first(open_app: OpenApp) -> None:
    variants = [
        EBS.replace("Windows", w) for w in ("Windows Server", "Microsoft Windows", "Windows 11", "Win")
    ]
    with open_app(**DUP) as client:
        for md in variants:
            upload(client, md, force=True)
        candidates = post_doc(client, EBS_EDITED).json()["candidates"]
    assert len(candidates) == 3
    sims = [c["similarity"] for c in candidates]
    assert sims == sorted(sims, reverse=True)


def test_exact_duplicate_is_decided_before_embedding(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder, **DUP) as client:
        first = upload(client, EBS)
        calls = len(embedder.passage_calls)
        resp = post_doc(client, EBS)
    assert resp.status_code == 409 and resp.json()["error"] == "duplicate"
    assert resp.json()["doc_id"] == first
    assert len(embedder.passage_calls) == calls  # no embedding for an exact duplicate


def test_models_off_skips_the_check_and_says_so(open_app: OpenApp) -> None:
    with open_app(models=False, **DUP) as client:
        upload(client, EBS)
        resp = post_doc(client, EBS_EDITED)
    assert resp.status_code == 201
    assert resp.json()["near_duplicate_check"] == SKIPPED


@pytest.mark.parametrize("mode", ["unhealthy", "raises"])
def test_embedder_trouble_skips_the_check_and_still_stores(open_app: OpenApp, mode: str) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder, **DUP) as client:
        upload(client, EBS)
        if mode == "unhealthy":
            embedder.healthy = False
        else:
            embedder.fail = True
        resp = post_doc(client, EBS_EDITED)
        nulls = scalar(client, "SELECT count(*) FROM chunks WHERE embedding IS NULL")
    assert resp.status_code == 201
    assert resp.json()["near_duplicate_check"] == SKIPPED
    assert nulls == resp.json()["chunks"]  # stored without vectors; a restart backfills them


def test_a_healthy_check_adds_no_note(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        body = post_doc(client, EBS).json()
    assert body["status"] == "created" and "near_duplicate_check" not in body


def test_chunks_are_embedded_once_and_stored_with_the_upload(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder, **DUP) as client:
        doc_id = upload(client, EBS)
        chunks = scalar(client, "SELECT count(*) FROM chunks WHERE doc_id = ?", doc_id)
        stored = scalar(client, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
        index_size = client.get("/healthz").json()["index_size"]
    assert len(embedder.passage_calls) == 1  # one batch for the whole doc, nothing twice
    assert embedder.passages_embedded == chunks == stored == index_size


def test_embedding_happens_before_the_write_transaction(open_app: OpenApp, db_path) -> None:
    """While the embedder runs, the database is neither written nor locked."""
    seen: list[tuple[int, bool]] = []

    def probe() -> None:
        other = db.connect(db_path)
        other.execute("PRAGMA busy_timeout=50")
        try:
            docs = int(other.execute("SELECT count(*) FROM docs").fetchone()[0])
            other.execute("BEGIN IMMEDIATE")  # fails if the service holds the write lock
            other.execute("ROLLBACK")
            seen.append((docs, True))
        except sqlite3.OperationalError:
            seen.append((-1, False))
        finally:
            other.close()

    with open_app(ProbeEmbedder(probe), **DUP) as client:
        upload(client, EBS)
    assert seen == [(0, True)]


def test_a_possible_duplicate_leaves_the_index_alone(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        upload(client, EBS)
        size = client.get("/healthz").json()["index_size"]
        post_doc(client, EBS_EDITED)
        assert client.get("/healthz").json()["index_size"] == size


def test_stored_vector_is_the_normalized_chunk_embedding(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        upload(client, DOCS["s3"][0])
        blobs = [bytes(r["embedding"]) for r in _rows(client)]
    vecs = np.stack([np.frombuffer(b, dtype=np.float32) for b in blobs])
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)


def _rows(client: TestClient):
    from tests.ops_helpers import rows

    return rows(client, "SELECT embedding FROM chunks ORDER BY id")


def test_concurrent_identical_uploads_store_one_doc(open_app: OpenApp) -> None:
    """The exact check is repeated inside the write transaction."""
    results: list[int] = []
    with open_app(**DUP) as client:
        gate = threading.Barrier(2)

        def go() -> None:
            gate.wait()
            results.append(client.post("/api/docs", json={"content": S3}, headers=AUTH).status_code)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = scalar(client, "SELECT count(*) FROM docs")
    assert sorted(results) == [201, 409] and total == 1
