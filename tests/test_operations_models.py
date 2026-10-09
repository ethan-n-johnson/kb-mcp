"""Phase 08 operations with the REAL bge-small embedder and cross-encoder (`pytest -m models`).

These cannot run in the cloud workspace (the ONNX models need HuggingFace); run
them on the laptop. Each test mirrors a fast test that runs the same code path
with the fakes:

- near-duplicate    -> tests/test_near_duplicate.py
- update            -> tests/test_update.py
- merge             -> tests/test_merge.py
- gap resolution    -> tests/test_gap_resolution.py
- ingest_url        -> tests/test_ingest_url.py

`KB_DUP_THRESHOLD` is set explicitly in every test. The near-duplicate test
calibrates it from the similarities bge actually produces (and prints them,
`pytest -s`), so it checks that the check can tell a lightly edited copy from
the other corpus docs, whatever the final tuned value (phase 11) turns out to be.
"""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import numpy as np
import pytest

from kb import db, service
from kb.config import Settings
from kb.ingest.chunk import chunk_markdown
from kb.search import indexing
from kb.search.dupes import doc_vector
from kb.search.embedder import Embedder, passage_text
from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS, EBS, QUERIES, S3
from tests.helpers import load_corpus, search, upload
from tests.ops_helpers import EBS_EDITED, LINUX_QUERY, add_gap, post_doc, rows
from tests.test_search_models import (  # noqa: F401  (fixtures and the wrapper are reused)
    Preloaded,
    model_dir,
    real_embedder,
    real_reranker,
    wrap,
)

pytestmark = pytest.mark.models


def doc_sim(embedder: Embedder, title: str, md: str, other_title: str, other_md: str) -> float:
    """Cosine of two documents' mean chunk vectors, computed the way the service does."""

    def vec(t: str, text: str) -> np.ndarray:
        chunks = chunk_markdown(text, t)
        return doc_vector(
            embedder.embed_passages([passage_text(t, c.heading_path, c.text) for c in chunks])
        )

    return float(vec(title, md) @ vec(other_title, other_md))


# --- near-duplicate -----------------------------------------------------------------------


def test_edited_copy_is_told_apart_from_unrelated_docs(
    open_app: OpenApp, wrap: Any, real_embedder: Embedder
) -> None:
    title = "Amazon EBS volumes"
    edited = doc_sim(real_embedder, title, EBS_EDITED, title, EBS)
    unrelated = {
        key: doc_sim(real_embedder, title, EBS_EDITED, key, md)
        for key, (md, _) in DOCS.items()
        if key != "ebs"
    }
    worst = max(unrelated.values())
    print(f"\nbge doc similarity: edited copy {edited:.4f}; unrelated {unrelated}")
    assert edited > worst + 0.02, "the edited copy must score clearly above every unrelated doc"
    threshold = round((edited + worst) / 2, 4)

    emb, rr = wrap()
    with open_app(emb, rr, KB_DUP_THRESHOLD=threshold) as client:
        ids = load_corpus(client)  # five unrelated docs: none is flagged as a near-duplicate
        before = len(rows(client, "SELECT id FROM docs"))
        flagged = post_doc(client, EBS_EDITED)
        assert flagged.status_code == 200
        body = flagged.json()
        forced = post_doc(client, EBS_EDITED, force=True)
        after = len(rows(client, "SELECT id FROM docs"))
    assert body["status"] == "possible_duplicate"
    assert body["candidates"][0]["doc_id"] == ids["ebs"]
    assert body["candidates"][0]["similarity"] == pytest.approx(edited, abs=0.01)
    assert before == 5 and forced.status_code == 201 and after == 6


def test_each_chunk_is_embedded_once_when_the_check_runs(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr, KB_DUP_THRESHOLD=0.99) as client:
        md, tags = DOCS["vpc"]
        upload(client, md, tags)
        chunks = client.get("/healthz").json()["chunks"]
    assert len(emb.passage_calls) == 1 and emb.passages_embedded == chunks


# --- update -------------------------------------------------------------------------------


def test_update_re_embeds_only_when_content_changes(open_app: OpenApp, wrap: Any) -> None:
    new_text = S3.replace(
        "## Versioning",
        "## Intelligent tiering\n\nThe Intelligent-Tiering storage class automatically moves each object "
        "between access tiers as its access pattern changes, so you pay the lowest price without writing "
        "lifecycle rules for data whose usage you cannot predict. A small monitoring charge applies per object "
        "and there is no retrieval fee when an object moves back to the frequent access tier.\n\n## Versioning",
    )
    emb, rr = wrap()
    with open_app(emb, rr, KB_DUP_THRESHOLD=0.99) as client:
        ids = load_corpus(client)
        calls = len(emb.passage_calls)
        assert client.patch(f"/api/docs/{ids['s3']}", json={"tags": ["archive"]}, headers=AUTH).status_code == 200
        assert len(emb.passage_calls) == calls  # tags-only: nothing embedded
        resp = client.patch(f"/api/docs/{ids['s3']}", json={"content": new_text}, headers=AUTH)
        assert resp.json()["rechunked"] is True and len(emb.passage_calls) == calls + 1
        body = search(client, "Which storage class automatically moves each object between access tiers as its access pattern changes?")
        size = client.get("/healthz").json()
    top = body["results"][0]
    assert body["mode"] == "semantic" and top["doc_id"] == ids["s3"]
    assert top["heading_path"].endswith("Intelligent tiering")
    assert size["index_size"] == size["chunks"]


# --- merge --------------------------------------------------------------------------------


def test_merge_makes_the_sources_answers_come_from_the_target(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    with open_app(emb, rr, KB_DUP_THRESHOLD=0.99) as client:
        ids = load_corpus(client)
        resp = client.post(
            "/api/docs/merge",
            json={"target_id": ids["vpc"], "source_ids": [ids["s3"]]},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = search(client, QUERIES["s3_lifecycle"][0])
        gone = client.get(f"/api/docs/{ids['s3']}").status_code
    assert gone == 404
    assert body["mode"] == "semantic" and body["results"][0]["doc_id"] == ids["vpc"]
    assert body["results"][0]["heading_path"].endswith("Lifecycle rules")


# --- gap resolution --------------------------------------------------------------------------


def test_a_gap_is_resolved_by_the_real_reranker(open_app: OpenApp, wrap: Any) -> None:
    emb, rr = wrap()
    # 0.5 rather than the 0.70 default: this checks the wiring, tuning is phase 11's job.
    with open_app(emb, rr, KB_DUP_THRESHOLD=0.99, KB_CONF_HIGH=0.5) as client:
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        add_gap(client, "airspeed velocity unladen swallow", "airspeed swallow")
        doc_id = upload(client, EBS)
        resolved = {r["query"]: r["resolved_doc_id"] for r in rows(client, "SELECT query, resolved_doc_id FROM gaps")}
    assert resolved[LINUX_QUERY] == doc_id
    assert resolved["airspeed velocity unladen swallow"] is None


# --- ingest_url ------------------------------------------------------------------------------


@pytest.fixture
def svc_models(db_path: Any, wrap: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A service-level environment with the real models (explicit settings, no HTTP)."""
    monkeypatch.delenv("KB_BUCKET", raising=False)
    settings = Settings(db_path=str(db_path), dup_threshold=0.99)
    conn = db.connect(db_path)
    db.init_schema(conn)
    emb, rr = wrap()
    state = indexing.build_state(settings, emb, rr)
    asyncio.run(indexing.start(state))
    yield SimpleNamespace(conn=conn, state=state, embedder=emb, reranker=rr)
    conn.close()


def test_ingest_url_embeds_and_indexes_the_page(
    svc_models: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port=None, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=EBS.encode(), headers={"content-type": "text/markdown"})
    )
    result = service.ingest_url(
        svc_models.conn, "https://docs.example.com/ebs.md", "t",
        search_state=svc_models.state, transport=transport,
    )
    assert result.status == "created" and result.near_duplicate_check is None
    assert len(svc_models.state.index) == result.chunks
