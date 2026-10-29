"""Snapshots of the database to S3: dirty marking, debounce, flush, failure handling.

Unit tests drive a `Snapshotter` with a recording fake S3 client; the route
tests run the real app against a moto bucket.
"""

from __future__ import annotations

import gzip
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from kb import backup, config, db, service, snapshot
from kb.config import Settings
from kb.ingest.fetch import FetchResult
from kb.snapshot import Snapshotter
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS, S3, VPC
from tests.helpers import upload
from tests.mcp_helpers import call, ok

BUCKET = "kb-snapshot-test"
SNAP_KEY = "snapshots/kb-latest.db.gz"
LONG = 3600  # an interval the thread never reaches, so only stop() uploads


class FakeS3:
    """Records `upload_file` calls and keeps a copy of each uploaded file."""

    def __init__(self, fail_first: int = 0) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.fail_first = fail_first
        self.attempts = 0
        self.gate: threading.Event | None = None  # when set, an upload waits for it
        self.entered = threading.Event()
        self.lock = threading.Lock()

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, str] | None = None) -> None:
        with self.lock:
            self.attempts += 1
            attempt = self.attempts
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if attempt <= self.fail_first:
            raise ConnectionError("S3 is unreachable")
        assert bucket == BUCKET and ExtraArgs == {"ServerSideEncryption": "AES256"}
        with self.lock:
            self.uploads.append((key, Path(filename).read_bytes()))

    def titles(self, index: int = -1, tmp: Path | None = None) -> list[str]:
        """Doc titles inside the upload at `index`."""
        assert tmp is not None
        copy = tmp / f"copy{index}.db"
        copy.write_bytes(gzip.decompress(self.uploads[index][1]))
        conn = sqlite3.connect(copy)
        try:
            return [r[0] for r in conn.execute("SELECT title FROM docs ORDER BY id")]
        finally:
            conn.close()


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


@pytest.fixture
def live(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = db.connect(db_path)
    db.init_schema(conn)
    yield conn
    conn.close()


def add_doc(conn: sqlite3.Connection, title: str) -> None:
    from kb.schemas import UploadRequest

    body = f"# {title}\n\n{title} is described here with enough words to be stored as a chunk."
    service.upload(conn, UploadRequest(content=body, title=title), "tester")


def make(db_path: Path, fake: FakeS3, interval: float = 0.15) -> Snapshotter:
    return Snapshotter(db_path, BUCKET, interval=interval, client=fake)


# --- debounce, flush, retry (fake S3) -----------------------------------------------------


def test_a_burst_of_writes_makes_one_upload(db_path: Path, live: sqlite3.Connection) -> None:
    fake = FakeS3()
    snap = make(db_path, fake, interval=0.3)
    snap.start()
    try:
        for n in range(25):
            add_doc(live, f"Doc {n}")
        wait_for(lambda: len(fake.uploads) >= 1)
        time.sleep(0.9)  # three more intervals with nothing to do
    finally:
        snap.stop()
    assert len(fake.uploads) == 1 and fake.uploads[0][0] == SNAP_KEY
    assert snap.last is not None and snap.last.docs == 25 and not snap.dirty


def test_an_idle_database_uploads_nothing(db_path: Path, live: sqlite3.Connection) -> None:
    fake = FakeS3()
    snap = make(db_path, fake, interval=0.05)
    snap.start()
    time.sleep(0.4)
    snap.stop()
    assert fake.uploads == [] and fake.attempts == 0


def test_a_write_during_an_upload_is_covered_by_the_next_snapshot(
    db_path: Path, live: sqlite3.Connection, tmp_path: Path
) -> None:
    fake = FakeS3()
    fake.gate = threading.Event()
    snap = make(db_path, fake, interval=0.05)
    snap.start()
    try:
        add_doc(live, "First")
        assert fake.entered.wait(10)  # the first upload is running and held at the gate
        add_doc(live, "Second")  # commits while the copy is already taken
        fake.gate.set()
        wait_for(lambda: len(fake.uploads) == 2)
    finally:
        snap.stop()
    assert fake.titles(0, tmp_path) == ["First"]
    assert fake.titles(1, tmp_path) == ["First", "Second"]


def test_stop_flushes_a_pending_snapshot(db_path: Path, live: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeS3()
    snap = make(db_path, fake, interval=LONG)
    snap.start()
    add_doc(live, "Late write")
    assert snap.dirty and fake.uploads == []  # still inside the debounce window
    snap.stop()
    assert len(fake.uploads) == 1 and fake.titles(0, tmp_path) == ["Late write"]
    assert not snap.dirty


def test_stop_without_pending_writes_uploads_nothing_and_twice_is_safe(
    db_path: Path, live: sqlite3.Connection
) -> None:
    fake = FakeS3()
    snap = make(db_path, fake, interval=LONG)
    snap.start()
    snap.stop()
    snap.stop()
    assert fake.attempts == 0


def test_writes_to_another_database_do_not_mark_this_one(
    db_path: Path, live: sqlite3.Connection, tmp_path: Path
) -> None:
    snap = make(db_path, FakeS3(), interval=LONG)
    snap.start()
    try:
        other = db.connect(tmp_path / "other" / "kb.db")
        db.init_schema(other)
        add_doc(other, "Elsewhere")
        other.close()
        assert not snap.dirty
        add_doc(live, "Here")
        assert snap.dirty
    finally:
        snap.stop(flush=False)


def test_a_failed_upload_is_retried_and_never_raised(
    db_path: Path, live: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeS3(fail_first=2)
    snap = make(db_path, fake, interval=0.05)
    caplog.set_level(logging.INFO, logger="kb.snapshot")
    snap.start()
    try:
        add_doc(live, "Doc")  # the write itself is not affected by S3 being down
        wait_for(lambda: len(fake.uploads) == 1)
    finally:
        snap.stop()
    assert fake.attempts == 3 and snap.failures == 2 and not snap.dirty
    failed = [r for r in caplog.records if r.getMessage() == "snapshot failed"]
    assert len(failed) == 2 and all(r.retry is True for r in failed)  # type: ignore[attr-defined]


def test_a_failed_final_flush_is_logged_not_raised(
    db_path: Path, live: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeS3(fail_first=99)
    snap = make(db_path, fake, interval=LONG)
    snap.start()
    add_doc(live, "Doc")
    snap.stop()  # does not raise
    assert fake.uploads == [] and snap.failures == 1 and snap.dirty
    assert any(r.getMessage() == "snapshot failed" for r in caplog.records)


def test_an_unexpected_error_does_not_kill_the_thread(
    db_path: Path, live: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeS3()
    calls = {"n": 0}
    real = snapshot.create_snapshot

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot, "create_snapshot", flaky)
    snap = make(db_path, fake, interval=0.05)
    snap.start()
    try:
        add_doc(live, "Doc")
        wait_for(lambda: len(fake.uploads) == 1)
    finally:
        snap.stop()
    assert snap.failures == 1


def test_a_damaged_database_is_never_uploaded(db_path: Path, live: sqlite3.Connection) -> None:
    for n in range(30):
        add_doc(live, f"Doc {n}")
    live.close()
    from tests.test_backup import damage_page, root_page

    damage_page(db_path, page=root_page(db_path, "chunks"))
    fake = FakeS3()
    snap = make(db_path, fake, interval=LONG)
    snap.mark_dirty()
    snap.start()
    snap.stop()
    assert fake.uploads == [] and snap.failures == 1


def test_each_snapshot_logs_key_bytes_and_milliseconds(
    db_path: Path, live: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="kb.snapshot")
    snap = make(db_path, FakeS3(), interval=LONG)
    snap.start()
    add_doc(live, "Doc")
    snap.stop()
    (line,) = [r for r in caplog.records if r.getMessage() == "snapshot uploaded"]
    assert line.key == SNAP_KEY and line.bytes > 0 and line.ms >= 0 and line.docs == 1  # type: ignore[attr-defined]


def test_a_missing_database_file_is_skipped(tmp_path: Path) -> None:
    snap = make(tmp_path / "none" / "kb.db", FakeS3())
    assert snap.snapshot_now() is None


# --- settings -----------------------------------------------------------------------------


def test_snapshots_need_a_bucket_and_are_on_by_default() -> None:
    assert Settings().snapshots_enabled is False
    assert Settings(bucket="b").snapshots_enabled is True
    assert Settings(bucket="b", snapshots=False).snapshots_enabled is False
    assert Settings().snapshot_interval == 5.0


def test_snapshot_settings_come_from_the_environment() -> None:
    env = {"KB_BUCKET": "b", "KB_SNAPSHOTS": "off", "KB_SNAPSHOT_INTERVAL": "2.5"}
    s = Settings.from_env(env)
    assert (s.snapshots, s.snapshot_interval, s.snapshots_enabled) == (False, 2.5, False)
    assert Settings.from_env({"KB_BUCKET": "b", "KB_SNAPSHOTS": " ON "}).snapshots_enabled is True
    assert Settings.from_env({"KB_BUCKET": "b", "KB_SNAPSHOT_INTERVAL": ""}).snapshot_interval == 5.0


@pytest.mark.parametrize("bad", ["0", "-1", "nan"])
def test_a_bad_interval_stops_startup(bad: str) -> None:
    with pytest.raises(ValueError, match="KB_SNAPSHOT_INTERVAL"):
        Settings.from_env({"KB_SNAPSHOT_INTERVAL": bad})


def test_from_settings_returns_none_when_snapshots_are_off() -> None:
    assert snapshot.from_settings(Settings()) is None  # no bucket (local dev)
    assert snapshot.from_settings(Settings(bucket="b", snapshots=False)) is None
    made = snapshot.from_settings(Settings(bucket="b", snapshot_interval=7.0))
    assert made is not None and made.interval == 7.0 and made.bucket == "b"
    # PostgreSQL mode: nothing to copy, whatever the bucket says
    assert snapshot.from_settings(Settings(bucket="b", database_url="postgresql://u:p@h/d")) is None


# --- the real app against moto ------------------------------------------------------------


@pytest.fixture
def bucket() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def snapshots_of(client: TestClient) -> Snapshotter:
    snap = client.app.state.snapshots
    assert snap is not None
    return snap  # type: ignore[no-any-return]


def clean(client: TestClient) -> None:
    """Forget pending writes (the setup uploads), so a test sees only its own action."""
    snap = snapshots_of(client)
    with snap._lock:
        snap._dirty = False
    assert not snap.dirty


def snapshot_titles(s3: Any, tmp_path: Path) -> list[str]:
    body = gzip.decompress(s3.get_object(Bucket=BUCKET, Key=SNAP_KEY)["Body"].read())
    copy = tmp_path / "snapshot-copy.db"
    copy.write_bytes(body)
    conn = sqlite3.connect(copy)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM docs ORDER BY id")]
    finally:
        conn.close()


def snapshot_app(open_app: OpenApp, **env: Any) -> Any:
    return open_app(KB_BUCKET=BUCKET, KB_SNAPSHOT_INTERVAL=LONG, **env)


Ids = dict[str, int]
Action = Callable[[TestClient, Ids], Any]
HX_AUTH = {"HX-Request": "true", **AUTH}


def mcp(tool: str, args: Callable[[Ids], dict[str, Any]]) -> Action:
    return lambda c, ids: ok(call(c, tool, args(ids), token=True))


# Every way a client can change the database. Each runs against two stored docs
# (`a`, `b`) after the dirty flag was cleared, and must set it again.
WRITES: dict[str, Action] = {
    "rest upload": lambda c, ids: c.post("/api/docs", json={"content": EBS}, headers=AUTH),
    "rest upload file": lambda c, ids: c.post(
        "/api/docs/file", files={"file": ("n.md", EBS.encode(), "text/markdown")}, headers=AUTH
    ),
    "rest update tags": lambda c, ids: c.patch(f"/api/docs/{ids['a']}", json={"tags": ["t"]}, headers=AUTH),
    "rest update content": lambda c, ids: c.patch(
        f"/api/docs/{ids['a']}", json={"content": VPC + "\n\nOne more sentence about subnets."}, headers=AUTH
    ),
    "rest delete": lambda c, ids: c.delete(f"/api/docs/{ids['a']}", headers=AUTH),
    "rest merge": lambda c, ids: c.post(
        "/api/docs/merge", json={"target_id": ids["a"], "source_ids": [ids["b"]]}, headers=AUTH
    ),
    "rest verify": lambda c, ids: c.post(f"/api/docs/{ids['a']}/verify", json={"note": "n"}, headers=AUTH),
    "rest fallback toggle": lambda c, ids: c.post("/api/admin/fallback", json={"on": True}, headers=AUTH),
    "rest ingest": lambda c, ids: c.post("/api/ingest", json={"url": "https://docs.example.com/x.md"}, headers=AUTH),
    "search that logs a gap": lambda c, ids: c.get("/api/search", params={"q": "zyxwvu qqqq"}),
    "ui search that logs a gap": lambda c, ids: c.get(
        "/ui/search", params={"q": "zyxwvu qqqq"}, headers={"HX-Request": "true"}
    ),
    "ui verify": lambda c, ids: c.post(f"/ui/docs/{ids['a']}/verify", headers=HX_AUTH),
    "ui fallback toggle": lambda c, ids: c.post("/ui/admin/fallback", data={"on": "true"}, headers=HX_AUTH),
    "ui add paste": lambda c, ids: c.post("/ui/add/paste", data={"content": EBS}, headers=HX_AUTH),
    "ui add file": lambda c, ids: c.post(
        "/ui/add/file", files={"file": ("e.md", EBS.encode(), "text/markdown")}, headers=HX_AUTH
    ),
    "ui add url": lambda c, ids: c.post("/ui/add/url", data={"url": "https://docs.example.com/x.md"}, headers=HX_AUTH),
    "mcp upload": mcp("upload", lambda ids: {"title": "Note", "content": "# Note\n\nsome text long enough to chunk"}),
    "mcp update": mcp("update", lambda ids: {"doc_id": ids["a"], "title": "Renamed"}),
    "mcp delete": mcp("delete", lambda ids: {"doc_id": ids["a"]}),
    "mcp merge": mcp("merge", lambda ids: {"target_id": ids["a"], "source_ids": [ids["b"]]}),
    "mcp ingest_url": mcp("ingest_url", lambda ids: {"url": "https://docs.example.com/x.md"}),
    "mcp verify": mcp("verify", lambda ids: {"doc_id": ids["a"]}),
    "mcp search that logs a gap": lambda c, ids: call(c, "search", {"query": "zyxwvu qqqq"}),
}


@pytest.mark.parametrize("name", list(WRITES))
def test_every_write_route_marks_the_database_dirty(
    open_app: OpenApp, bucket: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    page = FetchResult("https://docs.example.com/x.md", "text/markdown", S3.encode())
    monkeypatch.setattr(service, "fetch_url", lambda url, transport=None: page)
    with snapshot_app(open_app) as client:
        ids = {"a": upload(client, VPC), "b": upload(client, "# Route 53\n\nRoute 53 is a DNS service with health checks.")}
        assert snapshots_of(client).dirty  # the setup writes were seen too
        clean(client)
        result = WRITES[name](client, ids)
        if hasattr(result, "status_code"):
            assert result.status_code in (200, 201), result.text
        assert snapshots_of(client).dirty, f"{name} did not mark the database dirty"


READS: dict[str, Action] = {
    "rest get": lambda c, ids: c.get(f"/api/docs/{ids['a']}"),
    "rest chunk": lambda c, ids: c.get("/api/chunks/1"),
    "rest tags": lambda c, ids: c.get("/api/tags"),
    "rest stale": lambda c, ids: c.get("/api/stale"),
    "rest gaps": lambda c, ids: c.get("/api/gaps"),
    "rest audit": lambda c, ids: c.get("/api/audit"),
    "search with a good answer": lambda c, ids: c.get("/api/search", params={"q": "Which settings does a VPC have?"}),
    "healthz": lambda c, ids: c.get("/healthz"),
    "ui home": lambda c, ids: c.get("/"),
    "ui doc page": lambda c, ids: c.get(f"/docs/{ids['a']}"),
    "ui maintenance": lambda c, ids: c.get("/maintenance"),
    "mcp get": lambda c, ids: call(c, "get", {"doc_id": ids["a"]}),
    "mcp list_tags": lambda c, ids: call(c, "list_tags"),
    "mcp stale": lambda c, ids: call(c, "stale"),
    "mcp gaps": lambda c, ids: call(c, "gaps"),
}


@pytest.mark.parametrize("name", list(READS))
def test_reads_do_not_mark_the_database_dirty(open_app: OpenApp, bucket: Any, name: str) -> None:
    with snapshot_app(open_app) as client:
        ids = {"a": upload(client, VPC)}
        clean(client)
        READS[name](client, ids)
        assert not snapshots_of(client).dirty, f"{name} marked the database dirty"


def test_requests_that_are_refused_or_fail_write_nothing_and_stay_clean(open_app: OpenApp, bucket: Any) -> None:
    with snapshot_app(open_app) as client:
        ids = {"a": upload(client, VPC)}
        clean(client)
        assert client.post("/api/docs", json={"content": EBS}).status_code == 401
        assert client.delete(f"/api/docs/{ids['a']}", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.delete("/api/docs/9999", headers=AUTH).status_code == 404
        assert client.post("/api/docs", json={"content": VPC}, headers=AUTH).status_code == 409  # duplicate
        assert client.post("/api/docs/merge", json={"target_id": ids["a"], "source_ids": [9999]}, headers=AUTH).status_code == 404
        assert not snapshots_of(client).dirty


def test_the_startup_backfill_marks_the_database_dirty(open_app: OpenApp, bucket: Any) -> None:
    with open_app(models=False) as client:  # stores chunks without vectors, no snapshots (no bucket)
        upload(client, VPC)
        assert client.app.state.snapshots is None
    with snapshot_app(open_app) as client:  # the backfill embeds them: UPDATE chunks
        assert client.get("/healthz").json()["index_size"] >= 1
        assert snapshots_of(client).dirty


def test_a_started_app_on_an_empty_database_is_clean(open_app: OpenApp, bucket: Any) -> None:
    with snapshot_app(open_app) as client:
        assert not snapshots_of(client).dirty
    assert "Contents" not in bucket.list_objects_v2(Bucket=BUCKET)  # no empty snapshot is created


def test_graceful_shutdown_flushes_the_last_write(open_app: OpenApp, bucket: Any, tmp_path: Path) -> None:
    with snapshot_app(open_app) as client:
        upload(client, VPC)
        assert "Contents" not in bucket.list_objects_v2(Bucket=BUCKET)  # still inside the debounce window
        assert snapshots_of(client).dirty
    # Leaving the context ran the lifespan shutdown (what SIGTERM triggers).
    assert snapshot_titles(bucket, tmp_path) == ["Amazon VPC networking"]
    head = bucket.head_object(Bucket=BUCKET, Key=SNAP_KEY)
    assert head["ServerSideEncryption"] == "AES256"


def test_the_background_thread_uploads_within_the_interval(open_app: OpenApp, bucket: Any, tmp_path: Path) -> None:
    with open_app(KB_BUCKET=BUCKET, KB_SNAPSHOT_INTERVAL=0.2) as client:
        upload(client, VPC)
        wait_for(lambda: "Contents" in bucket.list_objects_v2(Bucket=BUCKET))
        assert snapshot_titles(bucket, tmp_path) == ["Amazon VPC networking"]
        upload(client, EBS)
        wait_for(lambda: len(snapshot_titles(bucket, tmp_path)) == 2)
    versions = bucket.list_object_versions(Bucket=BUCKET, Prefix="snapshots/")  # moto buckets are unversioned
    assert versions["Versions"]


def test_snapshots_are_skipped_without_a_bucket(open_app: OpenApp) -> None:
    with open_app() as client:  # conftest removes KB_BUCKET
        assert client.app.state.snapshots is None
        assert client.post("/api/docs", json={"content": VPC}, headers=AUTH).status_code == 201


def test_snapshots_can_be_switched_off_with_a_bucket_set(open_app: OpenApp, bucket: Any) -> None:
    with open_app(KB_BUCKET=BUCKET, KB_SNAPSHOTS="off") as client:
        assert client.app.state.snapshots is None
        upload(client, VPC)
    assert "Contents" not in bucket.list_objects_v2(Bucket=BUCKET)


def test_an_s3_outage_does_not_fail_requests_or_shutdown(
    open_app: OpenApp, bucket: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(*args: Any, **kwargs: Any) -> Any:
        raise backup.BackupError("upload failed: S3 is down")

    monkeypatch.setattr(snapshot, "create_snapshot", down)
    with open_app(KB_BUCKET=BUCKET, KB_SNAPSHOT_INTERVAL=0.05) as client:
        for _ in range(3):
            assert client.post("/api/docs", json={"content": VPC + f"\n\nx{_}"}, headers=AUTH).status_code == 201
        wait_for(lambda: snapshots_of(client).failures >= 2)  # retried
        assert client.get("/healthz").status_code == 200
        assert client.post("/api/docs", json={"content": EBS}, headers=AUTH).status_code == 201
    # Exiting the context (shutdown flush against the dead S3) did not raise.


def test_a_snapshot_restores_to_the_same_documents(open_app: OpenApp, bucket: Any, tmp_path: Path) -> None:
    with snapshot_app(open_app) as client:
        upload(client, VPC)
        upload(client, EBS)
    target = tmp_path / "new-instance" / "kb.db"
    result = backup.restore_backup(BUCKET, target, latest=True, force=True, client=bucket)
    assert result.key == SNAP_KEY and result.docs == 2
    conn = sqlite3.connect(target)
    try:
        assert [r[0] for r in conn.execute("SELECT title FROM docs ORDER BY id")] == [
            "Amazon VPC networking",
            "Amazon EBS volumes",
        ]
        assert conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0] >= 2
    finally:
        conn.close()


def test_config_helpers_are_not_needed_by_the_snapshotter() -> None:
    # The snapshotter takes plain arguments; it does not read the environment itself.
    assert "environ" not in Path(snapshot.__file__).read_text(encoding="utf-8")
    assert config.Settings().snapshot_interval == 5.0
