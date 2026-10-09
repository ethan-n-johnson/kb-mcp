"""`merge`: sources become sections of the target; one transaction; index follows."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kb import service
from tests.conftest import AUTH, OpenApp, Svc
from tests.corpus import EBS, ELB, S3, VPC
from tests.fakes import FakeEmbedder
from tests.helpers import load_corpus, search, upload
from tests.ops_helpers import add_gap, audit_rows, get_doc, rows, scalar

S3_URL = "https://docs.example.com/s3/storage-classes"
ELB_URL = "https://docs.example.com/elb/health"


def merge(client: TestClient, target: int, sources: list[int], **extra: object):
    body = {"target_id": target, "source_ids": sources, **extra}
    return client.post("/api/docs/merge", json=body, headers=AUTH)


def two_sources(client: TestClient) -> dict[str, int]:
    """Target VPC plus sources S3 (storage) and ELB (networking) with source URLs."""
    return {
        "vpc": upload(client, VPC, ["networking", "security"]),
        "s3": upload(client, S3, ["storage"], source_url=S3_URL),
        "elb": upload(client, ELB, ["networking", "lb"], source_url=ELB_URL),
    }


def test_target_gets_source_sections_with_demoted_headings_and_urls(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        resp = merge(client, ids["vpc"], [ids["s3"], ids["elb"]])
        doc = get_doc(client, ids["vpc"])
    assert resp.status_code == 200
    md = doc["content_md"]
    assert md.startswith("# Amazon VPC networking")
    # One section per source, in order, each with its URL right under the heading.
    assert f"## Amazon S3 storage classes\n\nSource: {S3_URL}\n\n" in md
    assert f"## Elastic Load Balancing\n\nSource: {ELB_URL}\n\n" in md
    assert md.index("## Amazon S3 storage classes") < md.index("## Elastic Load Balancing")
    # The sources' own headings moved one level down.
    for demoted in ("### Lifecycle rules", "### Versioning", "### Health checks", "### Cross-zone load balancing"):
        assert f"\n{demoted}\n" in md
    assert "\n## Lifecycle rules\n" not in md  # not left at the old level
    assert md.count("# Amazon S3 storage classes") == 1  # the repeated H1 was dropped, not duplicated
    # Heading paths of the new chunks nest the source under the target.
    paths = [c["heading_path"] for c in doc["chunks"]]
    assert "Amazon VPC networking > Amazon S3 storage classes > Lifecycle rules" in paths
    assert "Amazon VPC networking > Elastic Load Balancing > Health checks" in paths
    assert doc["title"] == "Amazon VPC networking"


def test_result_and_sources_are_gone(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        body = merge(client, ids["vpc"], [ids["s3"], ids["elb"]]).json()
        statuses = [client.get(f"/api/docs/{ids[k]}").status_code for k in ("s3", "elb")]
        remaining = scalar(client, "SELECT count(*) FROM docs")
        chunks = get_doc(client, ids["vpc"])["chunk_count"]
    assert body == {
        "status": "merged", "doc_id": ids["vpc"], "merged": [ids["s3"], ids["elb"]],
        "deleted": [ids["s3"], ids["elb"]], "chunks": chunks,
    }
    assert statuses == [404, 404] and remaining == 1


def test_search_hits_the_target_for_the_sources_content(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        before = search(client, "Which S3 feature moves old objects to Glacier automatically?")
        merge(client, ids["vpc"], [ids["s3"], ids["elb"]])
        after = search(client, "Which S3 feature moves old objects to Glacier automatically?")
        health = client.get("/healthz").json()
        index = client.app.state.search.index
        index_docs = index.doc_ids()
        total_chunks = scalar(client, "SELECT count(*) FROM chunks")
    assert before["results"][0]["doc_id"] == ids["s3"]
    assert after["mode"] == "semantic"
    top = after["results"][0]
    assert top["doc_id"] == ids["vpc"] and top["heading_path"].endswith("Lifecycle rules")
    assert top["source_url"] is None  # the target's own URL; the source URL lives in the text
    assert all(r["doc_id"] == ids["vpc"] for r in after["results"])
    assert index_docs == {ids["vpc"]}  # the sources' vectors left the index
    assert health["index_size"] == health["chunks"] == total_chunks


def test_target_is_re_embedded_once_with_its_new_chunks(open_app: OpenApp) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        ids = two_sources(client)
        calls = len(embedder.passage_calls)
        merge(client, ids["vpc"], [ids["s3"]])
        chunk_count = get_doc(client, ids["vpc"], chunks=False)["chunk_count"]
    assert len(embedder.passage_calls) == calls + 1
    assert len(embedder.passage_calls[-1]) == chunk_count  # only the merged doc, in one batch


def test_keyword_search_finds_the_merged_text(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        ids = two_sources(client)
        merge(client, ids["vpc"], [ids["s3"]])
        body = search(client, "glacier lifecycle")
    assert body["mode"] == "keyword" and body["results"][0]["doc_id"] == ids["vpc"]


def test_tags_are_the_union_and_the_url_of_the_target_is_kept(open_app: OpenApp) -> None:
    with open_app() as client:
        vpc = upload(client, VPC, ["networking"], source_url="https://docs.example.com/vpc")
        s3 = upload(client, S3, ["storage", "networking"], source_url=S3_URL)
        merge(client, vpc, [s3])
        doc = get_doc(client, vpc, chunks=False)
    assert doc["tags"] == ["networking", "storage"]
    assert doc["source_url"] == "https://docs.example.com/vpc"


def test_delete_sources_false_keeps_them(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        body = merge(client, ids["vpc"], [ids["s3"]], delete_sources=False).json()
        s3_still = client.get(f"/api/docs/{ids['s3']}").status_code
        in_index = client.app.state.search.index.doc_ids()
    assert body["deleted"] == [] and body["merged"] == [ids["s3"]]
    assert s3_still == 200 and {ids["s3"], ids["vpc"]} <= in_index


def test_one_audit_row_per_participating_doc(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        before = len(audit_rows(client))
        merge(client, ids["vpc"], [ids["s3"], ids["elb"]])
        new = audit_rows(client)[before:]
    assert new == [
        ("test", "merge", ids["vpc"]),
        ("test", "merge_source", ids["s3"]),
        ("test", "merge_source", ids["elb"]),
    ]


def test_gaps_resolved_by_a_source_now_point_at_the_target(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        add_gap(client, "glacier transition?", resolved_doc_id=ids["s3"])
        add_gap(client, "unrelated?", resolved_doc_id=ids["vpc"])
        add_gap(client, "open question?")
        merge(client, ids["vpc"], [ids["s3"], ids["elb"]])
        resolved = {r["query"]: r["resolved_doc_id"] for r in rows(client, "SELECT query, resolved_doc_id FROM gaps")}
    assert resolved == {
        "glacier transition?": ids["vpc"], "unrelated?": ids["vpc"], "open question?": None
    }


def test_kept_sources_keep_their_resolved_gaps(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        add_gap(client, "glacier transition?", resolved_doc_id=ids["s3"])
        merge(client, ids["vpc"], [ids["s3"]], delete_sources=False)
        assert scalar(client, "SELECT resolved_doc_id FROM gaps") == ids["s3"]


def test_cannot_merge_a_doc_into_itself(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        before = get_doc(client, ids["vpc"])["content_md"]
        resp = merge(client, ids["vpc"], [ids["s3"], ids["vpc"]])
        unchanged = get_doc(client, ids["vpc"])["content_md"] == before
        count = scalar(client, "SELECT count(*) FROM docs")
    assert (resp.status_code, resp.json()["error"]) == (400, "bad_request")
    assert "itself" in resp.json()["detail"] and unchanged and count == 3


@pytest.mark.parametrize("which", ["target", "source", "both"])
def test_unknown_ids_are_404_and_change_nothing(open_app: OpenApp, which: str) -> None:
    with open_app() as client:
        ids = two_sources(client)
        audits = len(audit_rows(client))
        target = 998 if which in ("target", "both") else ids["vpc"]
        sources = [ids["s3"], 999] if which in ("source", "both") else [ids["s3"]]
        resp = merge(client, target, sources)
        total = scalar(client, "SELECT count(*) FROM docs")
        assert len(audit_rows(client)) == audits
    assert (resp.status_code, resp.json()["error"]) == (404, "not_found")
    assert "999" in resp.json()["detail"] or "998" in resp.json()["detail"]
    assert total == 3


def test_empty_source_list_is_rejected(open_app: OpenApp) -> None:
    with open_app() as client:
        vpc = upload(client, VPC)
        assert merge(client, vpc, []).status_code == 422


def test_repeated_source_ids_count_once(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = two_sources(client)
        body = merge(client, ids["vpc"], [ids["s3"], ids["s3"]]).json()
        md = get_doc(client, ids["vpc"], chunks=False)["content_md"]
    assert body["merged"] == [ids["s3"]] and md.count("## Amazon S3 storage classes") == 1


def test_a_failure_inside_the_transaction_rolls_everything_back(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_app() as client:
        ids = two_sources(client)
        before = get_doc(client, ids["vpc"])
        audits = audit_rows(client)

        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(service, "_replace_chunks", boom)
        with pytest.raises(RuntimeError):
            client.post(
                "/api/docs/merge",
                json={"target_id": ids["vpc"], "source_ids": [ids["s3"], ids["elb"]]},
                headers=AUTH,
            )
        monkeypatch.undo()
        after = get_doc(client, ids["vpc"])
        survivors = scalar(client, "SELECT count(*) FROM docs")
        still_indexed = client.app.state.search.index.doc_ids()
        assert audit_rows(client) == audits
    assert after == before and survivors == 3  # sources not deleted, target not changed
    assert still_indexed == set(ids.values())


def test_merge_that_reproduces_another_docs_content_is_refused(svc: Svc) -> None:
    """The merged text is hashed like any content; a clash with another doc is a 409."""
    target = service.upload(svc.conn, service.UploadRequest(content=EBS), "t", search_state=svc.state)
    source = service.upload(svc.conn, service.UploadRequest(content=S3), "t", search_state=svc.state)
    assert target.doc_id and source.doc_id
    from kb.ingest import dedupe, mdmerge

    merged = mdmerge.merge_markdown(EBS, [mdmerge.MergeSource("Amazon S3 storage classes", None, S3)])
    clash = service.upload(
        svc.conn, service.UploadRequest(content=merged, force=True), "t", search_state=svc.state
    )
    assert clash.status == "created"
    assert dedupe.content_hash(merged) == svc.conn.execute(
        "SELECT content_hash FROM docs WHERE id = ?", (clash.doc_id,)
    ).fetchone()[0]
    with pytest.raises(service.ServiceError) as err:
        service.merge(svc.conn, target.doc_id, [source.doc_id], "t", search_state=svc.state)
    assert err.value.code == "duplicate" and err.value.http_status == 409
    assert svc.conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 3


def test_merging_while_models_are_down_stores_null_vectors_and_drops_the_index(
    open_app: OpenApp,
) -> None:
    embedder = FakeEmbedder()
    with open_app(embedder) as client:
        ids = two_sources(client)
        embedder.healthy = False
        assert merge(client, ids["vpc"], [ids["s3"]]).status_code == 200
        nulls = scalar(client, "SELECT count(*) FROM chunks WHERE embedding IS NULL")
        target_chunks = scalar(client, "SELECT count(*) FROM chunks WHERE doc_id = ?", ids["vpc"])
        chunks = scalar(client, "SELECT count(*) FROM chunks")
        indexed = client.app.state.search.index.doc_ids()
    # Only the merged doc lost its vectors; the untouched ELB doc keeps its own.
    assert nulls == target_chunks < chunks
    assert indexed == {ids["elb"]}
    fresh = FakeEmbedder()
    with open_app(fresh) as client:  # a restart backfills the merged doc
        assert client.get("/healthz").json()["index_size"] == chunks


def test_many_sources_and_markdown_with_code_fences(open_app: OpenApp) -> None:
    code_doc = (
        "# Shell tips\n\nSome introduction that is long enough to count as text for the chunker "
        "and then some more words so that it passes the fifty word minimum for a chunk of its own, "
        "because short sections get merged into the next one and that is not what this test is about "
        "so here are a few more sentences of filler text.\n\n```bash\n# list volumes\naws ec2 describe-volumes\n```\n"
    )
    with open_app() as client:
        target = upload(client, EBS)
        source = upload(client, code_doc)
        merge(client, target, [source])
        md = get_doc(client, target, chunks=False)["content_md"]
    assert "```bash\n# list volumes\naws ec2 describe-volumes\n```" in md  # the comment was not demoted


def test_merge_over_the_corpus_keeps_search_working_for_other_docs(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        merge(client, ids["vpc"], [ids["errors"]])
        body = search(client, "How do I attach an EBS volume to an EC2 instance on Linux?")
    assert body["results"][0]["doc_id"] == ids["ebs"]
