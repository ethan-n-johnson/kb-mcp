"""`update`: tags-only edits, content and title edits with re-embedding, and the error cases."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kb import service
from kb.errors import ServiceError
from kb.ingest.errors import IngestError
from kb.search.embedder import passage_text
from tests.conftest import AUTH, OpenApp, Svc
from tests.corpus import EBS, S3
from tests.fakes import FakeEmbedder
from tests.helpers import db_conn, load_corpus, search, upload
from tests.ops_helpers import ProbeEmbedder, audit_rows, backdate, get_doc, rows, scalar

NEW_S3 = """# Amazon S3 storage classes

Amazon S3 stores objects in buckets. Intelligent-Tiering is a storage class that automatically moves each object between access tiers when its access pattern changes, so a team that cannot predict its traffic still pays the lowest price without writing any rules. There is a small monitoring charge per object and no retrieval fee.

## Object Lock

Object Lock uses a write once read many model to stop an object version from being deleted or overwritten for a fixed period or indefinitely. Compliance mode cannot be shortened even by the root user, which makes it suitable for regulated records that must be retained unchanged. Governance mode lets specially permitted users remove the lock when a legitimate need arises, and a legal hold keeps a version protected until someone explicitly lifts it.
"""

NO_H1 = """Quarterly planning notes for the platform team cover how we decide what to build next and who reviews each proposal before the work is scheduled onto the roadmap for the coming months of the year. The notes are kept short on purpose so that every engineer can read them in a few minutes before the meeting starts, and they are revised after each quarter so that the next group of planners starts from what the previous group learned.

## Review process

Every proposal is reviewed by two engineers from different teams and one product manager. Reviewers check the scope, the estimated cost, the operational burden after launch, and whether an existing service already covers the need. Approved proposals are scheduled in the next planning meeting, and rejected ones receive written feedback within a week so that the authors can improve them and submit them again.
"""


def patch(client: TestClient, doc_id: int, **body: object):
    return client.patch(f"/api/docs/{doc_id}", json=body, headers=AUTH)


def chunk_ids(client: TestClient, doc_id: int) -> list[int]:
    return [r["id"] for r in rows(client, "SELECT id FROM chunks WHERE doc_id = ? ORDER BY ord", doc_id)]


# --- tags only ------------------------------------------------------------------------


def test_tags_only_does_not_re_embed_or_touch_chunks(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        doc_id = upload(client, EBS, ["storage", "ec2"])
        backdate(client, doc_id, 1)  # so a touched updated_at would show
        before = get_doc(client, doc_id)
        calls = len(embedder.passage_calls)
        ids_before = chunk_ids(client, doc_id)
        resp = patch(client, doc_id, tags=["storage", "ebs", " new "])
        after = get_doc(client, doc_id)
        ids_after = chunk_ids(client, doc_id)
        index_size = client.get("/healthz").json()["index_size"]
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "updated", "doc_id": doc_id, "rechunked": False, "chunks": before["chunk_count"]
    }
    assert len(embedder.passage_calls) == calls  # the spy: no embedding at all
    assert ids_after == ids_before  # the same chunk rows
    assert after["tags"] == ["ebs", "new", "storage"]
    assert after["updated_at"] == before["updated_at"]  # tags-only touches doc_tags only
    assert after["content_md"] == before["content_md"]
    assert index_size == len(ids_after)


def test_empty_tag_list_clears_the_tags(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS, ["storage"])
        assert patch(client, doc_id, tags=[]).status_code == 200
        assert get_doc(client, doc_id, chunks=False)["tags"] == []


def test_tags_filter_follows_the_edit(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        patch(client, ids["s3"], tags=["archive"])
        found = search(client, "storage classes lifecycle", tags="archive", k=10)
        old = search(client, "storage classes lifecycle", tags="storage", k=10)
    assert {r["doc_id"] for r in found["results"]} == {ids["s3"]}
    assert ids["s3"] not in {r["doc_id"] for r in old["results"]}


def test_identical_edit_is_unchanged_and_unaudited(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        doc_id = upload(client, EBS, ["storage"])
        calls = len(embedder.passage_calls)
        body = get_doc(client, doc_id)
        for payload in (
            {"tags": ["storage"]},
            {"title": body["title"]},
            {"content": body["content_md"]},
            {"content": EBS.replace("\n\n", "\n\n\n")},  # whitespace only: same content hash
        ):
            resp = patch(client, doc_id, **payload)
            assert resp.status_code == 200 and resp.json()["status"] == "unchanged", payload
        assert len(embedder.passage_calls) == calls
        assert audit_rows(client) == [("test", "upload", doc_id)]


# --- content and title ------------------------------------------------------------------


def test_content_change_replaces_chunks_and_search_finds_the_new_text(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        ids = load_corpus(client)
        doc_id = ids["s3"]
        old_ids = chunk_ids(client, doc_id)
        old_hash = scalar(client, "SELECT content_hash FROM docs WHERE id = ?", doc_id)
        calls = len(embedder.passage_calls)
        backdate(client, doc_id, 1)
        before_updated = get_doc(client, doc_id, chunks=False)["updated_at"]
        resp = patch(client, doc_id, content=NEW_S3)
        new_ids = chunk_ids(client, doc_id)
        doc = get_doc(client, doc_id)
        q = "Which storage class moves objects between access tiers automatically?"
        hit = search(client, q)
        lock = search(client, "How does Object Lock stop an object from being deleted?")
        stale_hit = search(client, "Which S3 feature moves old objects to Glacier automatically?")
        total_chunks = scalar(client, "SELECT count(*) FROM chunks")
        health = client.get("/healthz").json()
    assert resp.json() == {"status": "updated", "doc_id": doc_id, "rechunked": True, "chunks": 2}
    assert not set(old_ids) & set(new_ids)  # chunks were replaced, not patched
    assert len(embedder.passage_calls) == calls + 1  # re-embedded once, as one batch
    assert len(embedder.passage_calls[-1]) == 2
    assert doc["content_md"].startswith("# Amazon S3 storage classes") and "Object Lock" in doc["content_md"]
    assert scalar(client, "SELECT content_hash FROM docs WHERE id = ?", doc_id) != old_hash
    assert doc["updated_at"] > before_updated
    assert hit["mode"] == "semantic" and hit["results"][0]["doc_id"] == doc_id
    assert "Intelligent-Tiering" in hit["results"][0]["snippet"]
    assert lock["results"][0]["heading_path"].endswith("Object Lock")
    # The old text is gone from every path (vector index and FTS5).
    assert all("Glacier" not in r["snippet"] for r in stale_hit["results"])
    assert health["index_size"] == health["chunks"] == total_chunks
    assert audit_rows(client)[-1] == ("test", "update", doc_id)


def test_content_change_is_searchable_by_keyword_too(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        doc_id = upload(client, S3)
        patch(client, doc_id, content=NEW_S3)
        body = search(client, "Intelligent-Tiering monitoring charge")
        gone = search(client, "Glacier")
    assert body["mode"] == "keyword" and body["results"][0]["doc_id"] == doc_id
    assert gone["results"] == []


def test_title_change_re_embeds_with_the_new_title_in_the_text(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        doc_id = upload(client, NO_H1, title="Old planning title")
        before = [c["heading_path"] for c in get_doc(client, doc_id)["chunks"]]
        calls = len(embedder.passage_calls)
        resp = patch(client, doc_id, title="Platform roadmap")
        doc = get_doc(client, doc_id)
        health = client.get("/healthz").json()
    assert before == ["Old planning title", "Review process"]
    assert resp.json()["rechunked"] is True
    assert doc["title"] == "Platform roadmap"
    # The first chunk's heading path is the title; every embedded text carries it.
    assert [c["heading_path"] for c in doc["chunks"]] == ["Platform roadmap", "Review process"]
    assert len(embedder.passage_calls) == calls + 1
    embedded = embedder.passage_calls[-1]
    assert embedded == [
        passage_text("Platform roadmap", c["heading_path"], c["text"]) for c in doc["chunks"]
    ]
    assert all(t.startswith("Platform roadmap") for t in embedded)
    assert health["index_size"] == len(doc["chunks"])


def test_title_and_tags_together(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS, ["a"])
        resp = patch(client, doc_id, title="  Block storage  ", tags=["b"])
        doc = get_doc(client, doc_id, chunks=False)
    assert resp.json()["rechunked"] is True
    assert doc["title"] == "Block storage" and doc["tags"] == ["b"]


def test_content_update_uses_markdown_cleaning_and_ignores_front_matter(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, S3, ["storage"])
        patch(client, doc_id, content="---\ntitle: Ignored\ntags: [x]\n---\n" + NEW_S3 + "\n\nSkip to content\n")
        doc = get_doc(client, doc_id, chunks=False)
    assert doc["title"] == "Amazon S3 storage classes" and doc["tags"] == ["storage"]
    assert "Skip to content" not in doc["content_md"] and "---" not in doc["content_md"]


def test_update_while_models_are_down_drops_stale_vectors_and_a_restart_repairs(
    open_app: OpenApp,
) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        doc_id = upload(client, S3)
        state = client.app.state.search
        embedder.healthy = False
        assert patch(client, doc_id, content=NEW_S3).status_code == 200
        nulls = scalar(client, "SELECT count(*) FROM chunks WHERE embedding IS NULL")
        in_index = doc_id in state.index.doc_ids()
    assert nulls == 2 and not in_index  # no stale vectors of the replaced chunks
    fresh = FakeEmbedder()
    with open_app(fresh) as client:  # startup backfill
        assert client.get("/healthz").json()["index_size"] == 2
        assert fresh.passages_embedded == 2
        assert search(client, "Object Lock compliance mode retention")["results"][0]["doc_id"] == doc_id


def test_embedder_failure_during_update_still_updates(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        doc_id = upload(client, S3)
        embedder.fail = True
        resp = patch(client, doc_id, content=NEW_S3)
        assert resp.status_code == 200 and resp.json()["rechunked"] is True
        assert "Object Lock" in get_doc(client, doc_id, chunks=False)["content_md"]


# --- errors ----------------------------------------------------------------------------


def test_update_errors(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        audits = len(audit_rows(client))
        nothing = patch(client, ids["s3"])
        blank = patch(client, ids["s3"], title="   ")
        missing = patch(client, 999, title="x")
        dup = patch(client, ids["s3"], content=EBS)
        empty = patch(client, ids["s3"], content="   \n")
        same_before = get_doc(client, ids["s3"])["content_md"]
        total = len(audit_rows(client))
    assert (nothing.status_code, nothing.json()["error"]) == (400, "bad_request")
    assert (blank.status_code, blank.json()["error"]) == (400, "bad_request")
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")
    assert (dup.status_code, dup.json()["error"]) == (409, "duplicate")
    assert str(ids["ebs"]) in dup.json()["detail"]
    assert (empty.status_code, empty.json()["error"]) == (422, "empty_content")
    assert same_before.startswith("# Amazon S3") and total == audits  # nothing written, nothing audited


def test_update_to_the_same_hash_as_itself_is_not_a_duplicate(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        resp = patch(client, doc_id, title="Renamed, same content")
    assert resp.status_code == 200 and resp.json()["rechunked"] is True


def test_a_concurrent_change_during_embedding_is_a_conflict_and_changes_nothing(
    open_app: OpenApp, db_path
) -> None:
    from kb import db

    fired: list[bool] = []

    def meddle() -> None:
        if fired:
            return
        fired.append(True)
        other = db.connect(db_path)
        try:  # someone else edits the doc while we embed
            other.execute("UPDATE docs SET content_hash = 'changed-by-someone-else' WHERE id = 1")
        finally:
            other.close()

    probe = ProbeEmbedder()
    with open_app(probe) as client:
        doc_id = upload(client, S3)
        old = chunk_ids(client, doc_id)
        probe.on_embed = meddle
        resp = patch(client, doc_id, content=NEW_S3)
        after = chunk_ids(client, doc_id)
        audits = audit_rows(client)
    assert (resp.status_code, resp.json()["error"]) == (409, "conflict")
    assert after == old and audits == [("test", "upload", doc_id)]


def test_service_level_update_without_a_search_state(svc: Svc) -> None:
    created = service.upload(svc.conn, service.UploadRequest(content=S3), "t")
    assert created.doc_id is not None
    result = service.update(svc.conn, created.doc_id, "t", content=NEW_S3)
    assert result.rechunked and result.chunks == 2
    assert svc.conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 2
    with pytest.raises(ServiceError) as err:
        service.update(svc.conn, created.doc_id, "t")
    assert err.value.code == "bad_request"
    with pytest.raises(IngestError):
        service.update(svc.conn, created.doc_id, "t", content="\n\n")


def test_old_chunks_leave_the_fts_index(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, S3)
        patch(client, doc_id, content=NEW_S3)
        conn = db_conn(client)
        try:
            old = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'glacier'").fetchone()[0]
            new = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'tiering'").fetchone()[0]
        finally:
            conn.close()
    assert (old, new) == (0, 1)
