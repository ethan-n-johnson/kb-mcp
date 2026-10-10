"""`kb.seed` and `python -m kb.cli seed`: manifest parsing, idempotent seeding, planted stale docs.

No real network: URL entries go through `httpx.MockTransport`, and DNS is faked
so the SSRF guard still runs. The real `seeds/seeds.yaml` is parsed and checked
for consistency with the eval questions, but never fetched here (that is the
laptop's `--dry-run`).
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from kb import cli, db, service
from kb.evaluate import load_questions
from kb.seed import Entry, Manifest, ManifestError, load_manifest, seed, summary
from tests.conftest import Svc
from tests.corpus import EBS, ELB, S3, VPC

ROOT = Path(__file__).resolve().parent.parent
HTML = (Path(__file__).parent / "fixtures" / "aws_like.html").read_bytes()
BASE = "https://docs.example.com/guide/"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every host resolves to a public address, so the SSRF guard passes for mock hosts."""

    def fake(host: str, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class Site:
    """A mock docs site: path -> (status, body, content type); counts requests."""

    def __init__(self, pages: dict[str, tuple[int, bytes, str]]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            status, body, ctype = self.pages.get(request.url.path, (404, b"missing", "text/plain"))
            return httpx.Response(status, content=body, headers={"content-type": ctype})

        return httpx.MockTransport(handler)


def _site() -> Site:
    return Site(
        {
            "/guide/ebs.md": (200, EBS.encode(), "text/markdown"),
            "/guide/s3.md": (200, S3.encode(), "text/markdown"),
            "/guide/attach.html": (200, HTML, "text/html; charset=utf-8"),
        }
    )


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "seeds.yaml"
    path.write_text(body, encoding="utf-8")
    return path


MANIFEST = f"""
delay_seconds: 0.25
sections:
  - tags: [aws]
    base_url: {BASE}
    entries:
      - url: ebs.md
        tags: [ebs]
      - url: s3.md
      - url: attach.html
        title: Attaching volumes
  - tags: [notes]
    entries:
      - path: local/elb.md
planted:
  stale_days: 200
  stale:
    - {BASE}s3.md
"""


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "elb.md").write_text(ELB, encoding="utf-8")
    return load_manifest(_write_manifest(tmp_path, MANIFEST))


def _no_sleep(_: float) -> None:
    return None


# --- manifest -----------------------------------------------------------------------


def test_manifest_joins_base_urls_and_merges_section_tags(manifest: Manifest, tmp_path: Path) -> None:
    assert [e.label for e in manifest.entries] == [
        f"{BASE}ebs.md",
        f"{BASE}s3.md",
        f"{BASE}attach.html",
        str(tmp_path / "local" / "elb.md"),
    ]
    assert manifest.entries[0].tags == ("aws", "ebs")
    assert manifest.entries[2].title == "Attaching volumes"
    assert manifest.entries[3].path == tmp_path / "local" / "elb.md"
    assert manifest.stale == [f"{BASE}s3.md"] and manifest.stale_days == 200
    assert manifest.delay_seconds == 0.25


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("sections: [{entries: [{url: 'https://a.example/x', path: y}]}]", "exactly one of"),
        ("sections: [{entries: [{tags: [a]}]}]", "exactly one of"),
        ("sections: [{entries: [{url: 'ftp://a.example/x'}]}]", "not an http(s) URL"),
        ("sections: [{entries: [{url: 'https://a.example/x'}, {url: 'https://a.example/x'}]}]", "listed twice"),
        ("sections: [{entries: [{url: 'https://a.example/x', tags: a}]}]", "tags must be a list"),
        ("sections: [{entries: [{url: 'https://a.example/x'}]}]\nplanted: {stale: ['https://b.example/']}", "not an entry"),
        ("sections: [{entries: [{url: 'https://a.example/x'}]}]\nplanted: {stale_days: 0}", "stale_days"),
        ("entries: []", "'sections' list"),
        ("sections: [{entries: [", "not valid YAML"),
    ],
)
def test_bad_manifests_name_the_problem(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ManifestError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_manifest(_write_manifest(tmp_path, body))


def test_missing_manifest_is_a_manifest_error(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(tmp_path / "nope.yaml")


# --- seeding --------------------------------------------------------------------------


def test_seed_stores_every_entry_through_the_upload_path(svc: Svc, manifest: Manifest) -> None:
    site = _site()
    outcomes = seed(svc.conn, manifest, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["added", "added", "added", "added", "stale"]
    rows = {r["source_url"]: r for r in svc.conn.execute("SELECT * FROM docs")}
    assert set(rows) == {f"{BASE}ebs.md", f"{BASE}s3.md", f"{BASE}attach.html", None}
    assert rows[f"{BASE}attach.html"]["title"] == "Attaching volumes"
    assert rows[f"{BASE}attach.html"]["source_type"] == "html"
    ebs_id = rows[f"{BASE}ebs.md"]["id"]
    tags = [r["tag"] for r in svc.conn.execute("SELECT tag FROM doc_tags WHERE doc_id = ? ORDER BY tag", (ebs_id,))]
    assert tags == ["aws", "ebs"]
    # Same write path as an upload: vectors stored and indexed, audited as `cli`.
    assert svc.conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 0
    assert len(svc.state.index) == svc.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    actors = {r[0] for r in svc.conn.execute("SELECT DISTINCT token_name FROM audit_log")}
    assert actors == {"cli"}


def test_rerun_adds_nothing_and_fetches_nothing(svc: Svc, manifest: Manifest) -> None:
    site = _site()
    seed(svc.conn, manifest, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    fetched = len(site.requests)
    again = seed(svc.conn, manifest, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    assert summary(again) == {"skipped": 5}
    assert len(site.requests) == fetched  # stored URLs are not fetched again
    assert svc.conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 4


def test_a_url_whose_content_is_already_stored_is_skipped(svc: Svc) -> None:
    site = Site({"/a.md": (200, EBS.encode(), "text/markdown"), "/b.md": (200, EBS.encode(), "text/markdown")})
    m = Manifest([Entry(url="https://docs.example.com/a.md"), Entry(url="https://docs.example.com/b.md")])
    outcomes = seed(svc.conn, m, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["added", "skipped"]
    assert outcomes[1].doc_id == outcomes[0].doc_id


def test_one_failing_entry_does_not_stop_the_run(svc: Svc, tmp_path: Path) -> None:
    site = _site()
    m = Manifest(
        [
            Entry(url=f"{BASE}missing.html"),
            Entry(url="http://169.254.169.254/latest/meta-data/"),
            Entry(path=tmp_path / "absent.md"),
            Entry(url=f"{BASE}ebs.md"),
        ]
    )
    outcomes = seed(svc.conn, m, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["failed", "failed", "failed", "added"]
    assert outcomes[0].detail.startswith("fetch_failed")
    assert outcomes[1].detail.startswith("blocked_address")
    assert outcomes[2].detail.startswith("bad_path")


def test_near_duplicates_do_not_block_seeding(svc: Svc, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages from one docs site look alike; the seeder stores them anyway (force)."""
    import dataclasses

    svc.state.settings = dataclasses.replace(svc.state.settings, dup_threshold=0.0)
    site = Site({"/a.md": (200, EBS.encode(), "text/markdown"), "/b.md": (200, VPC.encode(), "text/markdown")})
    m = Manifest([Entry(url="https://docs.example.com/a.md"), Entry(url="https://docs.example.com/b.md")])
    outcomes = seed(svc.conn, m, search_state=svc.state, transport=site.transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["added", "added"]


def test_source_url_is_the_manifest_url_even_after_a_redirect(svc: Svc) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.md":
            return httpx.Response(301, headers={"location": "/new.md"})
        return httpx.Response(200, content=EBS.encode(), headers={"content-type": "text/markdown"})

    m = Manifest([Entry(url="https://docs.example.com/old.md")])
    seed(svc.conn, m, search_state=svc.state, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    row = svc.conn.execute("SELECT source_url FROM docs").fetchone()
    assert row["source_url"] == "https://docs.example.com/old.md"


def test_delay_is_only_between_real_fetches(svc: Svc, manifest: Manifest) -> None:
    slept: list[float] = []
    seed(svc.conn, manifest, search_state=svc.state, transport=_site().transport(), sleep=slept.append)
    assert slept == [0.25, 0.25]  # three URLs, no delay before the first; the path needs none
    slept.clear()
    seed(svc.conn, manifest, search_state=svc.state, transport=_site().transport(), sleep=slept.append)
    assert slept == []  # nothing fetched on a rerun


def test_planted_stale_docs_show_up_in_stale_and_reruns_keep_them(svc: Svc, manifest: Manifest) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    seed(svc.conn, manifest, search_state=svc.state, transport=_site().transport(), sleep=_no_sleep, now=now)
    stale = service.stale(svc.conn, days=90)
    assert [d.source_url for d in stale.docs] == [f"{BASE}s3.md"]
    row = svc.conn.execute("SELECT * FROM docs WHERE source_url = ?", (f"{BASE}s3.md",)).fetchone()
    assert row["verified_by"] == "cli"
    assert row["updated_at"] == row["verified_at"] == (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = [r["note"] for r in svc.conn.execute("SELECT note FROM audit_log WHERE action = 'verify'")]
    assert notes == ["seed: backdated 200 days"]
    again = seed(
        svc.conn, manifest, search_state=svc.state, transport=_site().transport(), sleep=_no_sleep,
        now=now + timedelta(days=1),
    )
    assert again[-1].status == "skipped" and "already backdated" in again[-1].detail
    assert svc.conn.execute("SELECT count(*) FROM audit_log WHERE action = 'verify'").fetchone()[0] == 1


def test_stale_entry_that_failed_to_seed_is_reported(svc: Svc) -> None:
    url = f"{BASE}missing.html"
    m = Manifest([Entry(url=url)], stale=[url])
    outcomes = seed(svc.conn, m, search_state=svc.state, transport=_site().transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["failed", "failed"]
    assert "not seeded" in outcomes[1].detail


def test_dry_run_converts_but_writes_nothing(svc: Svc, manifest: Manifest) -> None:
    outcomes = seed(svc.conn, manifest, dry_run=True, transport=_site().transport(), sleep=_no_sleep)
    assert [o.status for o in outcomes] == ["ok"] * 4  # no planted step in a dry run
    assert "chunks" in outcomes[0].detail and "Amazon EBS volumes" in outcomes[0].detail
    assert svc.conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0


def test_seed_without_models_stores_chunks_for_the_backfill(conn, manifest: Manifest) -> None:  # type: ignore[no-untyped-def]
    outcomes = seed(conn, manifest, transport=_site().transport(), sleep=_no_sleep)
    assert summary(outcomes) == {"added": 4, "stale": 1}
    assert conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0] == 0


# --- CLI ------------------------------------------------------------------------------


def test_cli_seed_runs_end_to_end_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ebs.md").write_text(EBS, encoding="utf-8")
    (tmp_path / "docs" / "s3.md").write_text(S3, encoding="utf-8")
    path = _write_manifest(
        tmp_path, "sections:\n  - tags: [aws]\n    entries:\n      - path: docs/ebs.md\n      - path: docs/s3.md\n"
    )
    db_path = tmp_path / "data" / "kb.db"
    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "off")
    assert cli.main(["seed", str(path)]) == 0
    out = capsys.readouterr().out
    assert "summary: 2 added" in out and "documents in the database: 2" in out
    assert cli.main(["seed", str(path)]) == 0
    assert "summary: 2 skipped" in capsys.readouterr().out


def test_cli_seed_exits_1_when_an_entry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_manifest(tmp_path, "sections:\n  - entries:\n      - path: absent.md\n")
    monkeypatch.setenv("KB_DB", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_MODELS", "off")
    assert cli.main(["seed", str(path)]) == 1
    assert "1 failed" in capsys.readouterr().out


def test_cli_seed_dry_run_creates_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ebs.md").write_text(EBS, encoding="utf-8")
    path = _write_manifest(tmp_path, "sections:\n  - entries:\n      - path: ebs.md\n")
    db_path = tmp_path / "data" / "kb.db"
    monkeypatch.setenv("KB_DB", str(db_path))
    assert cli.main(["seed", str(path), "--dry-run"]) == 0
    assert "summary: 1 ok" in capsys.readouterr().out
    assert not db_path.exists()


def test_cli_seed_bad_manifest_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["seed", str(_write_manifest(tmp_path, "nope: 1"))]) == 2
    assert "sections" in capsys.readouterr().err


# --- the real manifest and eval questions ------------------------------------------------


def test_real_manifest_parses_and_covers_every_format() -> None:
    m = load_manifest(ROOT / "seeds" / "seeds.yaml")
    assert 55 <= len(m.entries) <= 70
    urls = [e.url or "" for e in m.entries]
    assert any(u.endswith(".pdf") for u in urls)
    assert any(u.endswith(".md") for u in urls)
    assert any(u.endswith(".html") for u in urls)
    assert len(m.stale) == 3
    # Nothing downloaded is committed: every entry is a URL.
    assert all(e.url is not None for e in m.entries)


def test_eval_questions_point_at_manifest_entries() -> None:
    m = load_manifest(ROOT / "seeds" / "seeds.yaml")
    urls = {e.url for e in m.entries}
    qset = load_questions(ROOT / "tests" / "eval" / "questions.yaml")
    kinds = [q.kind for q in qset.questions]
    assert kinds.count("outside") == 3 and kinds.count("paraphrase") == 3
    assert sum(1 for q in qset.questions if q.demo) == 2
    for q in qset.questions:
        for url in q.expect:
            assert url in urls, q.q
    assert qset.planted is not None and qset.planted.is_file()
    assert qset.planted_original in urls


def test_planted_near_duplicate_is_not_seeded_and_differs_from_any_entry() -> None:
    m = load_manifest(ROOT / "seeds" / "seeds.yaml")
    assert all("planted" not in (e.url or "") for e in m.entries)
    text = (ROOT / "seeds" / "planted" / "fastapi-near-duplicate.md").read_text(encoding="utf-8")
    assert text.startswith("# Handling Errors")
    assert "Our team runbook copy." in text


def test_dockerfile_copies_the_seeds_into_the_image() -> None:
    assert "COPY seeds/ ./seeds/" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_init_schema_is_enough_for_a_dry_run_connection() -> None:
    """The CLI dry run uses an in-memory connection without a schema; nothing may query it."""
    conn = db.connect(":memory:")
    try:
        outcomes = seed(conn, Manifest([Entry(url="https://docs.example.com/guide/ebs.md")]),
                        dry_run=True, transport=_site().transport(), sleep=_no_sleep)
    finally:
        conn.close()
    assert outcomes[0].status == "ok"
