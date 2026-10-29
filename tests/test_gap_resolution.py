"""Automatic gap resolution after upload, update and merge (fake models).

A gap is resolved when the reranker scores the question against the changed
doc's chunks at or above `KB_CONF_HIGH`. Resolution runs after the result of
the write is decided and can never fail the write.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kb.search import resolve
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS, ELB, S3, VPC
from tests.fakes import FakeEmbedder, FakeReranker
from tests.helpers import reopen, search, upload
from tests.ops_helpers import LINUX_QUERY, add_gap, get_doc, post_doc, rows, scalar

pytestmark = pytest.mark.dual

LIFECYCLE_Q = "Which S3 feature moves old objects to Glacier automatically?"


def resolved(client: TestClient) -> dict[str, int | None]:
    return {r["query"]: r["resolved_doc_id"] for r in rows(client, "SELECT query, resolved_doc_id FROM gaps")}


def test_a_search_gap_is_resolved_by_the_doc_that_answers_it(open_app: OpenApp) -> None:
    with open_app() as client:
        assert search(client, LINUX_QUERY)["confidence"] == "low"  # nothing stored yet: a gap
        assert resolved(client) == {LINUX_QUERY: None}
        doc_id = upload(client, EBS, ["storage"])
        gaps_default = client.get("/api/gaps").json()["gaps"]
        gaps_all = client.get("/api/gaps", params={"include_resolved": "true"}).json()["gaps"]
        answer = search(client, LINUX_QUERY)
    assert resolved(client) == {LINUX_QUERY: doc_id}
    assert gaps_default == []
    assert gaps_all[0]["resolved_doc_id"] == doc_id
    assert answer["confidence"] == "high"  # and the question really is answered now


def test_a_doc_that_does_not_answer_leaves_the_gap_open(open_app: OpenApp) -> None:
    with open_app() as client:
        search(client, LINUX_QUERY)
        upload(client, S3)
        upload(client, VPC)
    assert resolved(client) == {LINUX_QUERY: None}


def test_all_rows_of_a_normalized_query_resolve_together(open_app: OpenApp) -> None:
    with open_app() as client:
        for q in (LINUX_QUERY, LINUX_QUERY.lower(), "unrelated zebra question"):
            search(client, q)
        doc_id = upload(client, EBS)
        by_query = resolved(client)
    assert by_query[LINUX_QUERY] == doc_id and by_query[LINUX_QUERY.lower()] == doc_id
    assert by_query["unrelated zebra question"] is None


def test_only_the_new_docs_chunks_are_scored(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        upload(client, S3)  # an older doc that already exists
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        reranker.score_calls.clear()
        resp = post_doc(client, EBS)
        chunk_texts = [c["text"] for c in get_doc(client, resp.json()["doc_id"])["chunks"]]
    assert len(reranker.score_calls) == 1  # one question, one scoring call
    query, texts = reranker.score_calls[0]
    assert query == LINUX_QUERY
    assert len(texts) == len(chunk_texts) == resp.json()["chunks"]
    assert all(any(t.endswith(body) for body in chunk_texts) for t in texts)  # no S3 passages


def test_at_most_50_distinct_questions_most_asked_first(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        for i in range(60):
            add_gap(client, f"question number {i}", f"q{i}", at=f"2026-10-01T10:{i:02d}:00Z")
        for i in range(55, 60):  # the five most asked are the last five created
            add_gap(client, f"question number {i} again", f"q{i}", at="2026-10-02T10:00:00Z")
        reranker.score_calls.clear()
        upload(client, EBS)
    asked = {q for q, _ in reranker.score_calls}
    assert len(reranker.score_calls) == 50
    assert {f"question number {i} again" for i in range(55, 60)} <= asked  # repeats first, example is the latest
    assert "question number 0" not in asked  # the oldest single ask fell off the end


def test_already_resolved_questions_are_not_rescored(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        first = upload(client, S3)
        add_gap(client, LIFECYCLE_Q, "s3 lifecycle", resolved_doc_id=first)
        reranker.score_calls.clear()
        upload(client, ELB)
        assert reranker.score_calls == []
        assert scalar(client, "SELECT resolved_doc_id FROM gaps") == first


def test_the_first_answer_wins(open_app: OpenApp) -> None:
    with open_app() as client:
        search(client, LIFECYCLE_Q)
        first = upload(client, S3)
        upload(client, S3.replace("thirty days", "forty days"))  # a second doc that also answers it
        assert scalar(client, "SELECT resolved_doc_id FROM gaps") == first


def test_update_resolves_gaps_the_new_text_answers(open_app: OpenApp) -> None:
    new_text = S3.replace("## Versioning", "## Intelligent tiering\n\nThe Intelligent-Tiering class moves objects between access tiers automatically.\n\n## Versioning")
    q = "Which storage class moves objects between access tiers automatically?"
    with open_app() as client:
        doc_id = upload(client, S3)
        add_gap(client, q, "storage class moves objects access tiers automatically")
        assert resolved(client) == {q: None}
        resp = client.patch(f"/api/docs/{doc_id}", json={"content": new_text}, headers=AUTH)
        assert resp.status_code == 200
    assert resolved(client) == {q: doc_id}


def test_tags_only_update_does_not_run_resolution(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        doc_id = upload(client, EBS)
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        reranker.score_calls.clear()
        client.patch(f"/api/docs/{doc_id}", json={"tags": ["x"]}, headers=AUTH)
    assert reranker.score_calls == []
    assert resolved(client) == {LINUX_QUERY: None}


def test_merge_resolves_gaps_with_the_target(open_app: OpenApp) -> None:
    with open_app() as client:
        target = upload(client, VPC)
        source = upload(client, EBS)
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")  # a past, open gap
        resp = client.post(
            "/api/docs/merge", json={"target_id": target, "source_ids": [source]}, headers=AUTH
        )
        assert resp.status_code == 200
    assert resolved(client) == {LINUX_QUERY: target}


def test_resolution_is_skipped_when_models_are_unavailable(open_app: OpenApp) -> None:
    embedder, reranker = FakeEmbedder(), FakeReranker()
    with open_app(embedder, reranker) as client:
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        reranker.healthy = False
        upload(client, S3)
        assert reranker.score_calls == []
        reranker.healthy, embedder.healthy = True, False
        upload(client, VPC)
        assert reranker.score_calls == []
    assert resolved(client) == {LINUX_QUERY: None}
    with open_app(models=False) as client:
        post_doc(client, EBS)
    assert resolved(client) == {LINUX_QUERY: None}


def test_resolution_threshold_is_kb_conf_high(open_app: OpenApp) -> None:
    with open_app(KB_CONF_HIGH=0.9999) as client:
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        upload(client, EBS)
    assert resolved(client) == {LINUX_QUERY: None}


def test_rerank_off_uses_the_cosine_cutoff(open_app: OpenApp) -> None:
    with open_app(KB_RERANK="off", KB_COSINE_CONF_HIGH=0.3) as client:
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        add_gap(client, "zebra giraffe antelope", "zebra giraffe antelope")
        doc_id = upload(client, EBS)
    assert resolved(client) == {LINUX_QUERY: doc_id, "zebra giraffe antelope": None}
    with open_app(KB_RERANK="off", KB_COSINE_CONF_HIGH=0.99) as client:
        upload(client, ELB)  # nothing reaches 0.99
    assert resolved(client)["zebra giraffe antelope"] is None


# --- it can never fail a write -----------------------------------------------------------


@pytest.mark.parametrize("how", ["reranker_raises", "resolve_raises"])
def test_a_failure_never_fails_the_upload(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, how: str
) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker) as client:
        add_gap(client, LINUX_QUERY, "attach ebs volume linux instance")
        if how == "reranker_raises":
            reranker.fail = True
        else:
            def boom(*a: Any, **k: Any) -> None:
                raise RuntimeError("gap table exploded")

            monkeypatch.setattr(resolve, "resolve_gaps", boom)
        with caplog.at_level(logging.ERROR):
            resp = post_doc(client, EBS)
        assert resp.status_code == 201 and resp.json()["status"] == "created"
        doc_id = resp.json()["doc_id"]
        assert get_doc(client, doc_id, chunks=False)["title"] == "Amazon EBS volumes"
    assert any("gap resolution failed" in r.getMessage() for r in caplog.records)
    assert resolved(client) == {LINUX_QUERY: None}


def test_failures_never_fail_update_or_merge(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with open_app() as client:
        target = upload(client, VPC)
        source = upload(client, S3)
        other = upload(client, ELB)

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("nope")

        monkeypatch.setattr(resolve, "resolve_gaps", boom)
        with caplog.at_level(logging.ERROR):
            upd = client.patch(f"/api/docs/{other}", json={"title": "Renamed"}, headers=AUTH)
            mrg = client.post(
                "/api/docs/merge", json={"target_id": target, "source_ids": [source]}, headers=AUTH
            )
    assert upd.status_code == 200 and mrg.status_code == 200
    assert sum("gap resolution failed" in r.getMessage() for r in caplog.records) == 2


def test_resolution_runs_after_the_write_is_committed(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    """When it runs, the doc is already visible to other connections and audited."""
    seen: list[tuple[int, int]] = []
    real = resolve.resolve_gaps

    def spy(conn: Any, state: Any, doc_id: int) -> int:
        other = reopen(db_path)
        try:
            seen.append(
                (
                    int(other.execute("SELECT count(*) FROM docs WHERE id = ?", (doc_id,)).fetchone()[0]),
                    int(other.execute("SELECT count(*) FROM audit_log WHERE doc_id = ?", (doc_id,)).fetchone()[0]),
                )
            )
        finally:
            other.close()
        return real(conn, state, doc_id)

    monkeypatch.setattr(resolve, "resolve_gaps", spy)
    with open_app() as client:
        upload(client, EBS)
    assert seen == [(1, 1)]


def test_resolution_does_not_run_for_duplicates_or_possible_duplicates(open_app: OpenApp) -> None:
    reranker = FakeReranker()
    with open_app(reranker=reranker, KB_DUP_THRESHOLD=0.8) as client:
        upload(client, EBS)
        add_gap(client, LIFECYCLE_Q, "s3 lifecycle")
        reranker.score_calls.clear()
        assert post_doc(client, EBS).status_code == 409
        assert post_doc(client, EBS.replace("Windows", "Windows Server")).json()["status"] == "possible_duplicate"
    assert reranker.score_calls == []
