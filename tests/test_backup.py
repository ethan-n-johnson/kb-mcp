"""Backup and restore against a moto S3 bucket (phase 12)."""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
import pytest
from moto import mock_aws

from kb import cli, db, service
from kb.backup import (
    SNAPSHOT_KEY,
    BackupError,
    NothingToRestore,
    create_backup,
    create_snapshot,
    list_backups,
    restore_backup,
)
from kb.schemas import UploadRequest

BUCKET = "kb-backup-test"
KEY_PATTERN = re.compile(r"^backups/kb-\d{8}-\d{6}\.db\.gz$")


@pytest.fixture
def s3() -> Iterator[Any]:
    """A moto S3 client with an empty bucket; KB_BUCKET is set for CLI tests."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def make_db(path: Path, titles: list[str]) -> Path:
    """Create a kb database at `path` with one document per title."""
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        for title in titles:
            body = (
                f"# {title}\n\n"
                f"{title} is described here with enough words to be stored as a chunk. "
                f"The unique marker for this document is zq{abs(hash(title)) % 10**6}."
            )
            service.upload(conn, UploadRequest(content=body, title=title), "tester")
    finally:
        conn.close()
    return path


def doc_titles(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT title FROM docs ORDER BY id")]
    finally:
        conn.close()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def at(minute: int) -> datetime:
    return datetime(2026, 10, 11, 7, minute, 0, tzinfo=timezone.utc)


def put_gz(s3: Any, key: str, payload: bytes) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=gzip.compress(payload))


# --- backup -------------------------------------------------------------------------------


def test_backup_uploads_a_gzipped_copy_under_backups(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "live" / "kb.db", ["Alpha", "Beta"])
    result = create_backup(live, BUCKET, client=s3, now=at(15))
    assert result.key == "backups/kb-20261011-071500.db.gz"
    assert KEY_PATTERN.match(result.key) and result.docs == 2
    obj = s3.get_object(Bucket=BUCKET, Key=result.key)
    assert obj["ServerSideEncryption"] == "AES256"
    raw = gzip.decompress(obj["Body"].read())
    assert raw.startswith(b"SQLite format 3\x00")
    assert obj["ContentLength"] == result.bytes_uploaded


def test_backup_key_uses_the_current_utc_time_by_default(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    assert KEY_PATTERN.match(create_backup(live, BUCKET, client=s3).key)


def test_backup_is_consistent_with_another_connection_holding_a_read(
    tmp_path: Path, s3: Any
) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha", "Beta"])
    reader = sqlite3.connect(live)
    try:
        reader.execute("SELECT count(*) FROM docs").fetchall()
        create_backup(live, BUCKET, client=s3, now=at(15))
    finally:
        reader.close()
    assert len(list_backups(BUCKET, client=s3)) == 1


def test_backup_of_a_missing_database_fails_without_creating_it(tmp_path: Path, s3: Any) -> None:
    missing = tmp_path / "nope" / "kb.db"
    with pytest.raises(BackupError, match="not found"):
        create_backup(missing, BUCKET, client=s3)
    assert not missing.exists() and not missing.parent.exists()
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_backup_of_a_corrupt_database_uploads_nothing(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", [f"Doc {n}" for n in range(30)])
    damage_page(live, page=root_page(live, "chunks"))
    with pytest.raises(BackupError, match="integrity_check"):
        create_backup(live, BUCKET, client=s3)
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_backup_of_a_non_kb_sqlite_file_is_refused(tmp_path: Path, s3: Any) -> None:
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    with pytest.raises(BackupError, match="not a kb database"):
        create_backup(other, BUCKET, client=s3)


def test_backup_upload_failure_is_a_backup_error(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    with pytest.raises(BackupError, match="upload"):
        create_backup(live, "kb-bucket-that-does-not-exist", client=s3)


def test_list_backups_is_oldest_first_and_ignores_other_objects(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_backup(live, BUCKET, client=s3, now=at(30))
    create_backup(live, BUCKET, client=s3, now=at(15))
    s3.put_object(Bucket=BUCKET, Key="backups/notes.txt", Body=b"x")
    s3.put_object(Bucket=BUCKET, Key="originals/1/original.html", Body=b"x")
    assert list_backups(BUCKET, client=s3) == [
        "backups/kb-20261011-071500.db.gz",
        "backups/kb-20261011-073000.db.gz",
    ]


# --- restore ------------------------------------------------------------------------------


def test_backup_then_restore_round_trips_a_database_with_docs(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "data" / "kb.db", ["Alpha", "Beta", "Gamma"])
    key = create_backup(live, BUCKET, client=s3, now=at(15)).key
    # Lose the documents, as a bad delete would.
    conn = db.connect(live)
    conn.execute("DELETE FROM docs")
    conn.close()
    assert doc_titles(live) == []

    result = restore_backup(BUCKET, live, key=key, force=True, client=s3, now=at(40))

    assert doc_titles(live) == ["Alpha", "Beta", "Gamma"] and result.docs == 3
    assert result.previous == live.with_name("kb.db.pre-restore-20261011-074000")
    assert doc_titles(result.previous) == []  # the file that was replaced is kept
    # Chunks and the FTS index came back with the documents.
    conn = db.connect(live)
    try:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3
        hit = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'Beta'").fetchone()
        assert hit[0] >= 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    assert list(live.parent.glob(".kb-restore-*")) == []  # no temp files left


def test_restore_latest_picks_the_newest_backup(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_backup(live, BUCKET, client=s3, now=at(15))
    conn = db.connect(live)
    service.upload(conn, UploadRequest(content="# Beta\n\nSecond document body text.", title="Beta"), "t")
    conn.close()
    newest = create_backup(live, BUCKET, client=s3, now=at(45)).key
    target = tmp_path / "restored" / "kb.db"
    result = restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert result.key == newest and result.previous is None
    assert doc_titles(target) == ["Alpha", "Beta"]


def test_restore_by_key_picks_that_backup(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    first = create_backup(live, BUCKET, client=s3, now=at(15)).key
    conn = db.connect(live)
    service.upload(conn, UploadRequest(content="# Beta\n\nSecond document body text.", title="Beta"), "t")
    conn.close()
    create_backup(live, BUCKET, client=s3, now=at(45))
    target = tmp_path / "restored.db"
    restore_backup(BUCKET, target, key=first, force=True, client=s3)
    assert doc_titles(target) == ["Alpha"]


def test_restore_refuses_without_force_and_touches_nothing(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    key = create_backup(live, BUCKET, client=s3, now=at(15)).key
    before = sha(live)
    with pytest.raises(BackupError, match="--force"):
        restore_backup(BUCKET, live, key=key, client=s3)
    assert sha(live) == before and sorted(p.name for p in tmp_path.iterdir()) == ["kb.db"]


def test_restore_needs_exactly_one_of_key_or_latest(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    key = create_backup(live, BUCKET, client=s3, now=at(15)).key
    with pytest.raises(BackupError, match="exactly one"):
        restore_backup(BUCKET, live, force=True, client=s3)
    with pytest.raises(BackupError, match="exactly one"):
        restore_backup(BUCKET, live, key=key, latest=True, force=True, client=s3)


def test_restore_with_no_backups_or_a_wrong_key_fails_cleanly(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    before = sha(live)
    with pytest.raises(NothingToRestore, match="no snapshot"):
        restore_backup(BUCKET, live, latest=True, force=True, client=s3)
    with pytest.raises(BackupError, match="download"):
        restore_backup(BUCKET, live, key="backups/kb-19990101-000000.db.gz", force=True, client=s3)
    assert sha(live) == before


# --- snapshots and the restore source order -----------------------------------------------


def add_doc(path: Path, title: str) -> None:
    conn = db.connect(path)
    try:
        service.upload(
            conn, UploadRequest(content=f"# {title}\n\n{title} has enough words to be stored as a chunk.", title=title), "t"
        )
    finally:
        conn.close()


def test_snapshot_overwrites_one_fixed_key_and_matches_the_backup_format(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    first = create_snapshot(live, BUCKET, client=s3)
    add_doc(live, "Beta")
    second = create_snapshot(live, BUCKET, client=s3)
    assert first.key == second.key == SNAPSHOT_KEY == "snapshots/kb-latest.db.gz"
    assert (first.docs, second.docs) == (1, 2)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == [SNAPSHOT_KEY]  # one object, not a growing list
    obj = s3.get_object(Bucket=BUCKET, Key=SNAPSHOT_KEY)
    assert obj["ServerSideEncryption"] == "AES256"
    assert gzip.decompress(obj["Body"].read()).startswith(b"SQLite format 3\x00")
    assert list_backups(BUCKET, client=s3) == []  # a snapshot is not listed as a backup


def test_snapshot_of_a_corrupt_database_uploads_nothing(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", [f"Doc {n}" for n in range(30)])
    damage_page(live, page=root_page(live, "chunks"))
    with pytest.raises(BackupError, match="integrity_check"):
        create_snapshot(live, BUCKET, client=s3)
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_latest_prefers_the_snapshot_over_a_newer_looking_backup(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_backup(live, BUCKET, client=s3, now=at(15))
    add_doc(live, "Beta")
    create_snapshot(live, BUCKET, client=s3)
    create_backup(live, BUCKET, client=s3, now=at(50))  # newer than the snapshot object, still not preferred
    target = tmp_path / "new" / "kb.db"
    result = restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert result.key == SNAPSHOT_KEY
    assert doc_titles(target) == ["Alpha", "Beta"]


def test_latest_falls_back_to_the_newest_backup_without_a_snapshot(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_backup(live, BUCKET, client=s3, now=at(15))
    add_doc(live, "Beta")
    newest = create_backup(live, BUCKET, client=s3, now=at(45)).key
    target = tmp_path / "new" / "kb.db"
    result = restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert result.key == newest and doc_titles(target) == ["Alpha", "Beta"]


def test_latest_with_neither_raises_nothing_to_restore_and_creates_no_file(tmp_path: Path, s3: Any) -> None:
    target = tmp_path / "new" / "kb.db"
    with pytest.raises(NothingToRestore):
        restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert not target.exists()
    s3.put_object(Bucket=BUCKET, Key="originals/1/original.html", Body=b"x")  # other prefixes do not count
    with pytest.raises(NothingToRestore):
        restore_backup(BUCKET, target, latest=True, force=True, client=s3)


def test_a_damaged_snapshot_falls_back_to_the_newest_backup(
    tmp_path: Path, s3: Any, caplog: pytest.LogCaptureFixture
) -> None:
    live = make_db(tmp_path / "data" / "kb.db", ["Alpha"])
    newest = create_backup(live, BUCKET, client=s3, now=at(15)).key
    put_gz(s3, SNAPSHOT_KEY, corrupt_backups(tmp_path)["damaged page"])
    result = restore_backup(BUCKET, live, latest=True, force=True, client=s3, now=at(40))
    assert result.key == newest and doc_titles(live) == ["Alpha"]
    assert any("snapshot is damaged" in r.getMessage() for r in caplog.records)


def test_a_damaged_snapshot_and_no_backup_is_an_error_not_an_empty_start(tmp_path: Path, s3: Any) -> None:
    put_gz(s3, SNAPSHOT_KEY, b"garbage " * 100)
    target = tmp_path / "new" / "kb.db"
    with pytest.raises(BackupError, match="integrity_check") as info:
        restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert not isinstance(info.value, NothingToRestore) and not target.exists()


def test_a_failed_snapshot_download_does_not_fall_back_to_an_old_backup(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_backup(live, BUCKET, client=s3, now=at(15))
    create_snapshot(live, BUCKET, client=s3)
    real = s3.download_file

    def flaky(bucket: str, key: str, filename: str, **kw: Any) -> None:
        if key == SNAPSHOT_KEY:
            raise ConnectionError("network down")
        real(bucket, key, filename, **kw)

    monkeypatch.setattr(s3, "download_file", flaky)
    target = tmp_path / "new" / "kb.db"
    with pytest.raises(BackupError, match="download of .*kb-latest"):
        restore_backup(BUCKET, target, latest=True, force=True, client=s3)
    assert not target.exists()  # a transient error must not silently restore day-old data


def test_an_unreadable_bucket_is_an_error_not_an_empty_start(tmp_path: Path, s3: Any) -> None:
    target = tmp_path / "kb.db"
    with pytest.raises(BackupError) as info:
        restore_backup("kb-bucket-that-does-not-exist", target, latest=True, force=True, client=s3)
    assert not isinstance(info.value, NothingToRestore)


def test_restore_by_key_can_take_the_snapshot_key(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    create_snapshot(live, BUCKET, client=s3)
    target = tmp_path / "new.db"
    assert restore_backup(BUCKET, target, key=SNAPSHOT_KEY, force=True, client=s3).docs == 1


def root_page(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0])
    finally:
        conn.close()


def damage_page(path: Path, page: int) -> None:
    """Overwrite one whole database page with a repeating byte pattern."""
    size = int.from_bytes(path.read_bytes()[16:18], "big")
    with path.open("r+b") as fh:
        fh.seek((page - 1) * size)
        fh.write(b"\xa5" * size)


def corrupt_backups(tmp_path: Path) -> dict[str, bytes]:
    """Payloads that are valid gzip but not a healthy kb database."""
    good = make_db(tmp_path / "src" / "kb.db", [f"Doc {n}" for n in range(30)])
    bad_page = tmp_path / "src" / "bad.db"
    bad_page.write_bytes(good.read_bytes())
    # The chunks table is damaged; `docs` still reads fine, so only a full
    # integrity_check notices.
    damage_page(bad_page, page=root_page(bad_page, "chunks"))
    return {
        "garbage": b"this is not a database " * 200,
        "damaged page": bad_page.read_bytes(),
        "truncated database": good.read_bytes()[: 4096 * 2 + 100],
    }


@pytest.mark.parametrize("kind", ["garbage", "damaged page", "truncated database"])
def test_corrupted_backup_fails_the_check_and_leaves_the_current_db_untouched(
    tmp_path: Path, s3: Any, kind: str
) -> None:
    live = make_db(tmp_path / "data" / "kb.db", ["Alpha", "Beta"])
    before = sha(live)
    key = "backups/kb-20261011-071500.db.gz"
    put_gz(s3, key, corrupt_backups(tmp_path)[kind])
    with pytest.raises(BackupError, match="integrity_check failed"):
        restore_backup(BUCKET, live, key=key, force=True, client=s3)
    assert sha(live) == before
    assert doc_titles(live) == ["Alpha", "Beta"]
    assert sorted(p.name for p in live.parent.iterdir()) == ["kb.db"]  # no aside, no temp files


def test_backup_that_is_not_gzip_fails_and_leaves_the_current_db_untouched(
    tmp_path: Path, s3: Any
) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    before = sha(live)
    key = "backups/kb-20261011-071500.db.gz"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"plainly not gzip")
    with pytest.raises(BackupError, match="not a valid gzip"):
        restore_backup(BUCKET, live, key=key, force=True, client=s3)
    assert sha(live) == before


def test_truncated_gzip_stream_fails_and_leaves_the_current_db_untouched(
    tmp_path: Path, s3: Any
) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    before = sha(live)
    key = "backups/kb-20261011-071500.db.gz"
    packed = gzip.compress(live.read_bytes())
    s3.put_object(Bucket=BUCKET, Key=key, Body=packed[: len(packed) // 2])
    with pytest.raises(BackupError, match="not a valid gzip"):
        restore_backup(BUCKET, live, key=key, force=True, client=s3)
    assert sha(live) == before


def test_valid_but_empty_sqlite_file_is_not_restored(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "kb.db", ["Alpha"])
    before = sha(live)
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(empty)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    key = "backups/kb-20261011-071500.db.gz"
    put_gz(s3, key, empty.read_bytes())
    with pytest.raises(BackupError, match="not a kb database"):
        restore_backup(BUCKET, live, key=key, force=True, client=s3)
    assert sha(live) == before


def test_a_stale_rollback_journal_is_set_aside_not_replayed(tmp_path: Path, s3: Any) -> None:
    live = make_db(tmp_path / "data" / "kb.db", ["Alpha"])
    key = create_backup(live, BUCKET, client=s3, now=at(15)).key
    journal = live.with_name("kb.db-journal")
    journal.write_bytes(b"\x00" * 512)  # what a crash mid-write leaves behind
    result = restore_backup(BUCKET, live, key=key, force=True, client=s3, now=at(40))
    assert not journal.exists()
    assert result.previous is not None
    assert (live.parent / "kb.db.pre-restore-20261011-074000-journal").exists()
    assert doc_titles(live) == ["Alpha"]


def test_restore_swap_failure_puts_the_old_files_back(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from kb import backup

    live = make_db(tmp_path / "kb.db", ["Alpha"])
    key = create_backup(live, BUCKET, client=s3, now=at(15)).key
    before = sha(live)
    real_replace = os.replace

    def flaky(src: Any, dst: Any) -> None:
        if Path(dst) == live and Path(src).name == "kb.db":  # the final move into place
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", flaky)
    with pytest.raises(BackupError, match="swap in"):
        restore_backup(BUCKET, live, key=key, force=True, client=s3, now=at(40))
    monkeypatch.undo()
    assert sha(live) == before


# --- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    live = make_db(tmp_path / "data" / "kb.db", ["Alpha", "Beta"])
    monkeypatch.setenv("KB_DB", str(live))
    monkeypatch.setenv("KB_BUCKET", BUCKET)
    return live


def test_cli_backup_prints_only_the_key(cli_env: Path, s3: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backup"]) == 0
    out, err = capsys.readouterr()
    key = out.strip()
    assert KEY_PATTERN.match(key) and "\n" not in out.strip() and err == ""
    s3.head_object(Bucket=BUCKET, Key=key)


def test_cli_backup_without_a_bucket_exits_non_zero(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KB_BUCKET")
    assert cli.main(["backup"]) == 1
    assert "KB_BUCKET" in capsys.readouterr().err


def test_cli_backup_failure_exits_non_zero_with_a_timestamped_message(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KB_DB", str(cli_env.with_name("missing.db")))
    assert cli.main(["backup"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and re.match(r"^\d{4}-\d\d-\d\dT[\d:]+Z backup failed: ", err)


def test_cli_restore_without_force_refuses_before_any_s3_call(
    cli_env: Path, s3: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sha(cli_env)
    assert cli.main(["restore", "--latest"]) == 2
    assert "--force" in capsys.readouterr().err
    assert sha(cli_env) == before


def test_cli_restore_needs_latest_or_key(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["restore", "--force"])
    assert info.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["restore", "--force", "--latest", "--key", "backups/x"])


def test_cli_restore_latest_with_force_replaces_the_db(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["backup"]) == 0
    capsys.readouterr()
    conn = db.connect(cli_env)
    conn.execute("DELETE FROM docs")
    conn.close()
    assert cli.main(["restore", "--latest", "--force"]) == 0
    out = capsys.readouterr().out
    assert "docs:     2" in out and "pre-restore-" in out
    assert doc_titles(cli_env) == ["Alpha", "Beta"]


def test_cli_restore_latest_takes_the_snapshot_first(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backup"]) == 0  # two docs in the backup
    capsys.readouterr()
    add_doc(cli_env, "Gamma")
    create_snapshot(cli_env, BUCKET)  # three docs in the snapshot
    conn = db.connect(cli_env)
    conn.execute("DELETE FROM docs")
    conn.close()
    assert cli.main(["restore", "--latest", "--force"]) == 0
    out = capsys.readouterr().out
    assert f"restored: s3://{BUCKET}/{SNAPSHOT_KEY}" in out and "docs:     3" in out
    assert doc_titles(cli_env) == ["Alpha", "Beta", "Gamma"]


def test_cli_restore_latest_with_nothing_in_s3_exits_zero_and_starts_empty(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "data" / "kb.db"
    monkeypatch.setenv("KB_BUCKET", BUCKET)
    assert cli.main(["restore", "--latest", "--force", "--target", str(target)]) == 0
    out, err = capsys.readouterr()
    assert "nothing to restore" in out and "empty database" in out and err == ""
    assert not target.exists()


def test_cli_restore_latest_with_an_unreachable_bucket_exits_non_zero(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KB_BUCKET", "kb-bucket-that-does-not-exist")
    assert cli.main(["restore", "--latest", "--force", "--target", str(tmp_path / "kb.db")]) == 1
    assert "restore failed" in capsys.readouterr().err


def test_cli_restoring_a_backup_over_the_live_db_also_replaces_the_snapshot(
    cli_env: Path, s3: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["backup"]) == 0  # Alpha, Beta
    capsys.readouterr()
    add_doc(cli_env, "Gamma")
    create_snapshot(cli_env, BUCKET)  # the snapshot knows Gamma
    assert cli.main(["restore", "--key", sorted(list_backups(BUCKET, client=s3))[-1], "--force"]) == 0
    assert f"snapshot: s3://{BUCKET}/{SNAPSHOT_KEY}" in capsys.readouterr().out
    assert doc_titles(cli_env) == ["Alpha", "Beta"]
    # A replacement instance now restores the state the operator chose, not the one they undid.
    target = cli_env.parent / "replacement.db"
    assert restore_backup(BUCKET, target, latest=True, force=True, client=s3).key == SNAPSHOT_KEY
    assert doc_titles(target) == ["Alpha", "Beta"]


def test_cli_restore_to_a_scratch_target_leaves_the_snapshot_alone(
    cli_env: Path, s3: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["backup"])
    key = capsys.readouterr().out.strip()
    assert cli.main(["restore", "--key", key, "--target", str(cli_env.parent / "scratch.db"), "--force"]) == 0
    assert "snapshot:" not in capsys.readouterr().out
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="snapshots/")


def test_cli_restore_does_not_touch_the_snapshot_when_snapshots_are_off(
    cli_env: Path, s3: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["backup"])
    key = capsys.readouterr().out.strip()
    monkeypatch.setenv("KB_SNAPSHOTS", "off")
    assert cli.main(["restore", "--key", key, "--force"]) == 0
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="snapshots/")


def test_cli_restore_target_and_key(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["backup"])
    key = capsys.readouterr().out.strip()
    target = cli_env.parent / "elsewhere.db"
    assert cli.main(["restore", "--key", key, "--target", str(target), "--force"]) == 0
    assert doc_titles(target) == ["Alpha", "Beta"]
    assert doc_titles(cli_env) == ["Alpha", "Beta"]  # the live DB was not the target


def test_cli_restore_of_a_missing_key_exits_non_zero(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sha(cli_env)
    assert cli.main(["restore", "--key", "backups/kb-19990101-000000.db.gz", "--force"]) == 1
    assert "restore failed" in capsys.readouterr().err
    assert sha(cli_env) == before


def test_cli_help_lists_backup_and_restore(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "backup" in out and "restore" in out
