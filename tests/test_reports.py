"""`verify`, `stale`, `gaps`, `list_tags` and `audit`, through REST and the service."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kb import db, service
from kb.errors import ServiceError
from tests.conftest import AUTH, OpenApp, Svc
from tests.corpus import DOCS, EBS
from tests.helpers import load_corpus, search, upload
from tests.ops_helpers import add_gap, audit_rows, backdate, get_doc, rows

pytestmark = pytest.mark.dual


def verify(client: TestClient, doc_id: int, **body: object):
    return client.post(f"/api/docs/{doc_id}/verify", json=body or None, headers=AUTH)


# --- verify ------------------------------------------------------------------------------


def test_verify_sets_time_and_token_name_and_audits_the_note(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        before = get_doc(client, doc_id, chunks=False)
        resp = verify(client, doc_id, note="checked against the console")
        after = get_doc(client, doc_id, chunks=False)
        audit = rows(client, "SELECT token_name, action, doc_id, note FROM audit_log ORDER BY id DESC LIMIT 1")[0]
    assert before["verified_at"] is None and before["verified_by"] is None
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "verified" and body["doc_id"] == doc_id
    assert body["verified_by"] == "test" and body["verified_at"] == after["verified_at"]
    assert after["verified_at"].endswith("Z") and after["verified_by"] == "test"
    assert after["updated_at"] == before["updated_at"]  # verifying is not an edit
    assert tuple(audit) == ("test", "verify", doc_id, "checked against the console")


def test_verify_without_a_body_or_note(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        assert client.post(f"/api/docs/{doc_id}/verify", headers=AUTH).status_code == 200
        assert verify(client, doc_id, note="   ").status_code == 200
        notes = [r["note"] for r in rows(client, "SELECT note FROM audit_log WHERE action = 'verify'")]
    assert notes == [None, None]


def test_verify_errors(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        missing = verify(client, 404)
        too_long = verify(client, doc_id, note="x" * 501)
        total = len(audit_rows(client))
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")
    assert too_long.status_code == 422
    assert total == 1  # just the upload


def test_service_rejects_a_long_note_with_400(svc: Svc) -> None:
    created = service.upload(svc.conn, service.UploadRequest(content=EBS), "t")
    with pytest.raises(ServiceError) as err:
        service.verify(svc.conn, created.doc_id or 0, "t", note="x" * 501)
    assert err.value.http_status == 400


# --- stale -------------------------------------------------------------------------------


def stale(client: TestClient, **params: object) -> list[dict]:
    resp = client.get("/api/stale", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["docs"]


def test_verify_then_stale_zero_orders_by_last_touch(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        backdate(client, ids["ebs"], 300)
        backdate(client, ids["s3"], 200)
        backdate(client, ids["elb"], 50)
        backdate(client, ids["vpc"], 10)
        backdate(client, ids["errors"], 5)
        order_before = [d["doc_id"] for d in stale(client, days=0, limit=50)]
        verify(client, ids["ebs"])  # the oldest doc becomes the freshest
        after = stale(client, days=0, limit=50)
    assert order_before == [ids["ebs"], ids["s3"], ids["elb"], ids["vpc"], ids["errors"]]
    assert [d["doc_id"] for d in after] == [ids["s3"], ids["elb"], ids["vpc"], ids["errors"], ids["ebs"]]
    assert after[-1]["verified_at"] is not None and after[-1]["days_since"] == 0
    assert [d["days_since"] for d in after[:4]] == [200, 50, 10, 5]


def test_a_doc_backdated_200_days_is_stale_at_90(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        backdate(client, ids["s3"], 200)
        backdate(client, ids["vpc"], 91)
        backdate(client, ids["elb"], 89)
        docs = stale(client, days=90)
    assert [d["doc_id"] for d in docs] == [ids["s3"], ids["vpc"]]
    assert docs[0]["days_since"] == 200 and docs[0]["title"] == "Amazon S3 storage classes"
    assert set(docs[0]) == {"doc_id", "title", "source_url", "updated_at", "verified_at", "days_since"}


def test_a_recent_verification_keeps_an_old_doc_fresh(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        backdate(client, doc_id, 400)
        assert [d["doc_id"] for d in stale(client)] == [doc_id]
        verify(client, doc_id)
        assert stale(client) == []
        backdate(client, doc_id, 120, column="verified_at")  # an old verification does not hide it
        assert [d["days_since"] for d in stale(client)] == [120]


def test_stale_defaults_limit_and_validation(open_app: OpenApp) -> None:
    with open_app() as client:
        for key in ("ebs", "s3", "elb"):
            doc_id = upload(client, DOCS[key][0])
            backdate(client, doc_id, 100 + len(key))
        assert len(stale(client)) == 3  # default days=90
        assert len(stale(client, limit=2)) == 2
        assert client.get("/api/stale", params={"days": -1}).status_code == 422
        assert client.get("/api/stale", params={"limit": 0}).status_code == 422
        assert client.get("/api/stale", params={"limit": 201}).status_code == 422


def test_service_stale_rejects_negative_days(svc: Svc) -> None:
    with pytest.raises(ServiceError) as err:
        service.stale(svc.conn, days=-1)
    assert err.value.http_status == 400
    assert service.stale(svc.conn, days=0).docs == []


def test_stale_needs_no_token(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.get("/api/stale").status_code == 200


# --- gaps --------------------------------------------------------------------------------


def gaps(client: TestClient, **params: object) -> list[dict]:
    resp = client.get("/api/gaps", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["gaps"]


def test_gaps_group_by_normalized_query_with_counts(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        for q in (
            "What is the quantum tractor beam?",
            "what is the QUANTUM tractor beam",
            "How is the quantum tractor beam?",
            "Warp drive coolant pressure?",
        ):
            assert search(client, q)["confidence"] == "low"
        found = gaps(client)
    assert [(g["query_norm"], g["count"]) for g in found] == [
        ("quantum tractor beam", 3), ("warp drive coolant pressure", 1)
    ]
    top = found[0]
    assert top["query"] == "How is the quantum tractor beam?"  # the latest one asked
    assert set(top) == {"query_norm", "query", "count", "top_score", "last_seen", "resolved_doc_id"}
    assert top["resolved_doc_id"] is None


def test_gaps_order_and_best_score_and_last_seen(open_app: OpenApp) -> None:
    with open_app() as client:
        add_gap(client, "alpha?", "alpha", top_score=0.10, at="2026-10-01T10:00:00Z")
        add_gap(client, "alpha again", "alpha", top_score=0.25, at="2026-10-03T10:00:00Z")
        add_gap(client, "alpha third", "alpha", top_score=0.05, at="2026-10-02T10:00:00Z")
        add_gap(client, "beta?", "beta", top_score=0.2, at="2026-10-04T10:00:00Z")
        add_gap(client, "gamma?", "gamma", top_score=0.2, at="2026-10-05T10:00:00Z")
        add_gap(client, "delta?", "delta", top_score=None, at="2026-10-05T10:00:00Z")
        found = gaps(client)
        limited = gaps(client, limit=2)
    order = [g["query_norm"] for g in found]
    assert order[0] == "alpha" and set(order[1:3]) == {"gamma", "delta"} and order[3] == "beta"
    # Same count: the most recently seen first (gamma and delta tie on time, newest row first).
    assert order[1:] == ["delta", "gamma", "beta"]
    alpha = found[0]
    assert alpha["count"] == 3 and alpha["top_score"] == 0.25
    assert alpha["last_seen"] == "2026-10-03T10:00:00Z" and alpha["query"] == "alpha third"
    assert found[1]["top_score"] is None
    assert len(limited) == 2


def test_resolved_gaps_are_hidden_unless_asked_for(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        add_gap(client, "open one", "open one")
        add_gap(client, "closed one", "closed one", resolved_doc_id=doc_id)
        add_gap(client, "closed one again", "closed one", resolved_doc_id=doc_id)
        add_gap(client, "closed one, asked after the fix", "closed one")
        default = gaps(client)
        everything = gaps(client, include_resolved=True)
    assert {g["query_norm"]: g["count"] for g in default} == {"open one": 1, "closed one": 1}
    assert all(g["resolved_doc_id"] is None for g in default)
    by_norm = {g["query_norm"]: g for g in everything}
    assert by_norm["closed one"]["count"] == 3 and by_norm["closed one"]["resolved_doc_id"] == doc_id
    assert by_norm["open one"]["resolved_doc_id"] is None


def test_gaps_validation_and_no_token(open_app: OpenApp) -> None:
    with open_app() as client:
        assert gaps(client) == []
        assert client.get("/api/gaps", params={"limit": 0}).status_code == 422
        assert client.get("/api/gaps", params={"limit": 500}).status_code == 422


# --- tags --------------------------------------------------------------------------------


def test_list_tags_counts_and_order(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.get("/api/tags").json() == {"tags": []}
        load_corpus(client)  # storage x2, ec2 x2, networking x2, security x1
        tags = client.get("/api/tags").json()["tags"]
    assert tags == [
        {"tag": "ec2", "count": 2},
        {"tag": "networking", "count": 2},
        {"tag": "storage", "count": 2},
        {"tag": "security", "count": 1},
    ]


def test_tag_counts_follow_deletes_and_edits(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        client.delete(f"/api/docs/{ids['s3']}", headers=AUTH)
        client.patch(f"/api/docs/{ids['errors']}", json={"tags": ["ops"]}, headers=AUTH)
        tags = {t["tag"]: t["count"] for t in client.get("/api/tags").json()["tags"]}
    assert tags == {"networking": 2, "storage": 1, "ec2": 1, "security": 1, "ops": 1}


# --- audit -------------------------------------------------------------------------------


def test_audit_lists_newest_first_with_token_names_only(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        verify(client, doc_id, note="ok")
        client.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        resp = client.get("/api/audit")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert [e["action"] for e in entries] == ["fallback_on", "verify", "upload"]
    assert entries[0] == {
        "id": entries[0]["id"], "token_name": "test", "action": "fallback_on",
        "doc_id": None, "at": entries[0]["at"], "note": None,
    }
    assert entries[1]["note"] == "ok" and entries[1]["doc_id"] == doc_id
    assert entries[0]["id"] > entries[1]["id"] > entries[2]["id"]
    assert "testtoken" not in resp.text  # names, never secrets


def test_audit_limit(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        default = client.get("/api/audit").json()["entries"]
        two = client.get("/api/audit", params={"limit": 2}).json()["entries"]
        bad = [client.get("/api/audit", params={"limit": n}).status_code for n in (0, 201)]
    assert len(default) == 5 and len(two) == 2 and bad == [422, 422]
    assert two == default[:2]


def test_audit_needs_no_token_and_service_clamps(svc: Svc) -> None:
    assert service.audit(svc.conn).entries == []
    for i in range(3):
        svc.conn.execute(
            "INSERT INTO audit_log (token_name, action, doc_id, at) VALUES ('t', 'x', ?, ?)",
            (i, db.utcnow()),
        )
    assert [e.doc_id for e in service.audit(svc.conn, limit=0).entries] == [2]  # clamped up to 1
    assert len(service.audit(svc.conn, limit=10_000).entries) == 3
