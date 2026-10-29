"""SQLite backup and snapshot to S3, and restore from S3.

`create_backup` copies the live database with the SQLite online backup API
(safe while the app writes), checks the copy with `PRAGMA integrity_check`,
gzips it and uploads it to `backups/kb-YYYYMMDD-HHMMSS.db.gz` (the nightly
backup, kept 30 days). `create_snapshot` does the same but overwrites the single
key `snapshots/kb-latest.db.gz`; the app calls it a few seconds after every
write (`kb/snapshot.py`) and bucket versioning keeps the history.

`restore_backup` downloads one object, checks it, and only then swaps it in,
keeping the old file as `<name>.pre-restore-<timestamp>`. With `latest=True` the
source order is: the snapshot, else the newest backup, else
`NothingToRestore` (the caller starts with an empty database). Every failure
raises `BackupError`; a failed restore never touches the current database.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("kb.backup")

PREFIX = "backups/"
SNAPSHOT_KEY = "snapshots/kb-latest.db.gz"
KEY_RE = re.compile(r"^backups/kb-\d{8}-\d{6}\.db\.gz$")
# Sidecar files SQLite may leave next to a database. A rollback journal that
# survives next to the old name would be replayed into the restored file.
_SIDECARS = ("-journal", "-wal", "-shm")


class BackupError(Exception):
    """A backup or restore step failed; the message is safe to print."""


class CorruptBackup(BackupError):
    """The downloaded object is not a healthy kb database (bad gzip, failed integrity check)."""


class NothingToRestore(BackupError):
    """`restore_backup(latest=True)` found neither a snapshot nor a backup."""


@dataclass(frozen=True)
class BackupResult:
    """What `create_backup` produced."""

    key: str
    bytes_uploaded: int
    docs: int


@dataclass(frozen=True)
class RestoreResult:
    """What `restore_backup` did."""

    key: str
    target: Path
    previous: Path | None
    docs: int


def _s3(client: Any | None, region: str | None) -> Any:
    if client is not None:
        return client
    import boto3  # lazy: the CLI's other commands never need AWS

    return boto3.client("s3", region_name=region or os.environ.get("AWS_REGION") or "us-east-1")


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")


def _check_database(path: Path) -> int:
    """Run `PRAGMA integrity_check` and a schema sanity check; return the doc count.

    Raises `BackupError` unless the single result row is `ok` and the file has
    the kb `docs` table (an empty but valid SQLite file would otherwise pass).
    """
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            if [tuple(r) for r in rows] != [("ok",)]:
                first = str(rows[0][0]) if rows else "no result"
                raise CorruptBackup(f"integrity_check failed: {first}")
            has_docs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'docs'"
            ).fetchone()
            if has_docs is None:
                raise CorruptBackup("not a kb database: table docs is missing")
            return int(conn.execute("SELECT count(*) FROM docs").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise CorruptBackup(f"integrity_check failed: {exc}") from exc


def _packed_copy(source: Path, tmp: Path) -> tuple[Path, int, int]:
    """Copy `source` with the online backup API, check the copy, gzip it into `tmp`.

    Returns `(gzip path, gzip size, doc count)`. The copy, not the live file, is
    integrity checked, so a damaged database is never uploaded.
    """
    if not source.is_file():
        raise BackupError(f"database not found: {source}")
    copy = tmp / "kb.db"
    try:
        src = sqlite3.connect(str(source), timeout=30)
        try:
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise BackupError(f"could not copy the database: {exc}") from exc
    docs = _check_database(copy)
    packed = tmp / "kb.db.gz"
    with copy.open("rb") as raw, gzip.open(packed, "wb", compresslevel=6) as out:
        shutil.copyfileobj(raw, out)
    return packed, packed.stat().st_size, docs


def _upload_copy(
    source: Path, bucket: str, key: str, client: Any | None, region: str | None
) -> BackupResult:
    """Pack `source` and upload it to `key` (server-side encrypted)."""
    with tempfile.TemporaryDirectory(prefix="kb-backup-") as tmp:
        packed, size, docs = _packed_copy(source, Path(tmp))
        try:
            _s3(client, region).upload_file(
                str(packed), bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"}
            )
        except Exception as exc:  # boto raises many types; all mean "not uploaded"
            raise BackupError(f"upload to s3://{bucket}/{key} failed: {exc}") from exc
    return BackupResult(key=key, bytes_uploaded=size, docs=docs)


def create_backup(
    db_path: str | Path,
    bucket: str,
    *,
    client: Any | None = None,
    region: str | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Back up the live database at `db_path` to `s3://bucket/backups/...`.

    The copy is made with `sqlite3.Connection.backup()`, which takes a
    consistent snapshot while other connections keep working (writers wait up
    to the busy timeout). The copy, not the live file, is integrity checked
    and gzipped. Returns the S3 key. Raises `BackupError` on any failure and
    uploads nothing unless the copy is healthy.
    """
    key = f"{PREFIX}kb-{_stamp(now)}.db.gz"
    result = _upload_copy(Path(db_path), bucket, key, client, region)
    log.info(
        "backup uploaded",
        extra={"bucket": bucket, "key": key, "bytes": result.bytes_uploaded, "docs": result.docs},
    )
    return result


def create_snapshot(
    db_path: str | Path,
    bucket: str,
    *,
    client: Any | None = None,
    region: str | None = None,
) -> BackupResult:
    """Upload a consistent snapshot of the live database to `snapshots/kb-latest.db.gz`.

    Same copy, integrity check and gzip as `create_backup`, but the key is fixed:
    each call replaces the object, and bucket versioning keeps the previous
    versions. The caller (`kb/snapshot.py`) logs the result.
    """
    return _upload_copy(Path(db_path), bucket, SNAPSHOT_KEY, client, region)


def list_backups(bucket: str, *, client: Any | None = None, region: str | None = None) -> list[str]:
    """Return the backup keys under `backups/`, oldest first.

    Names embed a UTC timestamp, so sorting by key sorts by time. Keys that do
    not match the backup naming scheme are ignored.
    """
    s3 = _s3(client, region)
    keys: list[str] = []
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
            keys.extend(obj["Key"] for obj in page.get("Contents", []) if KEY_RE.match(obj["Key"]))
    except Exception as exc:
        raise BackupError(f"could not list s3://{bucket}/{PREFIX}: {exc}") from exc
    return sorted(keys)


def restore_backup(
    bucket: str,
    target: str | Path,
    *,
    key: str | None = None,
    latest: bool = False,
    force: bool = False,
    client: Any | None = None,
    region: str | None = None,
    now: datetime | None = None,
) -> RestoreResult:
    """Replace the database at `target` with an object from S3.

    Give exactly one of `key` or `latest=True`. Refuses without `force=True`,
    because replacing the file under a running app corrupts its view of it;
    callers stop the app first.

    With `latest=True` the source order is the snapshot (`snapshots/kb-latest.db.gz`),
    else the newest nightly backup, else `NothingToRestore`. A snapshot that
    downloads but is damaged (bad gzip, failed integrity check) falls back to the
    newest backup, logged as a warning; a failed download or listing does not
    (a transient S3 error must not silently restore day-old data), it raises.

    The object is downloaded next to `target`, gunzipped and integrity checked
    before anything is moved. Then the current file (and any journal beside it)
    is renamed to `<target>.pre-restore-<ts>` and the restored file takes its
    place. Any failure before the swap leaves the current database untouched.
    """
    if not force:
        raise BackupError("refusing to restore without --force (stop the app first)")
    if (key is None) == (not latest):
        raise BackupError("give exactly one of a key or latest")
    s3 = _s3(client, region)
    dest = Path(target)
    if key is not None:
        return _restore_key(s3, bucket, key, dest, now)
    candidates = _latest_candidates(s3, bucket)
    if not candidates:
        raise NothingToRestore(
            f"no snapshot under s3://{bucket}/{SNAPSHOT_KEY} and no backups under s3://{bucket}/{PREFIX}"
        )
    for index, candidate in enumerate(candidates):
        try:
            return _restore_key(s3, bucket, candidate, dest, now)
        except CorruptBackup as exc:
            if index == len(candidates) - 1:
                raise
            log.warning(
                "snapshot is damaged, trying the newest backup",
                extra={"key": candidate, "error": str(exc)},
            )
    raise AssertionError("unreachable")  # pragma: no cover


def _snapshot_exists(s3: Any, bucket: str) -> bool:
    """True when the snapshot object exists; a missing object is False, any other error raises."""
    try:
        s3.head_object(Bucket=bucket, Key=SNAPSHOT_KEY)
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise BackupError(f"could not look for s3://{bucket}/{SNAPSHOT_KEY}: {exc}") from exc
    return True


def _latest_candidates(s3: Any, bucket: str) -> list[str]:
    """Keys to try for `--latest`, best first: the snapshot (if any), then the newest backup (if any)."""
    keys: list[str] = []
    if _snapshot_exists(s3, bucket):
        keys.append(SNAPSHOT_KEY)
    backups = list_backups(bucket, client=s3)
    if backups:
        keys.append(backups[-1])
    return keys


def fetch_database(s3: Any, bucket: str, key: str, directory: Path) -> tuple[Path, int]:
    """Download `key`, gunzip it into `directory` and check it; return `(path, doc count)`.

    Raises `BackupError` when the download fails (including a missing object) and
    `CorruptBackup` when the file is not gzip or fails the integrity check.
    """
    packed = directory / "kb.db.gz"
    fresh = directory / "kb.db"
    try:
        s3.download_file(bucket, key, str(packed))
    except Exception as exc:
        raise BackupError(f"download of s3://{bucket}/{key} failed: {exc}") from exc
    try:
        with gzip.open(packed, "rb") as gz, fresh.open("wb") as out:
            shutil.copyfileobj(gz, out)
    except (OSError, EOFError) as exc:  # BadGzipFile is an OSError
        raise CorruptBackup(f"backup {key} is not a valid gzip file: {exc}") from exc
    return fresh, _check_database(fresh)


def upload_database(
    source: Path, bucket: str, key: str, *, client: Any | None = None, region: str | None = None
) -> BackupResult:
    """Check, gzip and upload the SQLite file `source` to `s3://bucket/key` (encrypted)."""
    return _upload_copy(source, bucket, key, client, region)


def _restore_key(s3: Any, bucket: str, key: str, dest: Path, now: datetime | None) -> RestoreResult:
    """Download `key`, verify it and swap it in as `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Same directory as the target so the final rename never crosses filesystems.
    with tempfile.TemporaryDirectory(prefix=".kb-restore-", dir=dest.parent) as tmp:
        fresh, docs = fetch_database(s3, bucket, key, Path(tmp))
        previous = _swap_in(fresh, dest, _stamp(now))
    log.info("restored", extra={"key": key, "target": str(dest), "docs": docs})
    return RestoreResult(key=key, target=dest, previous=previous, docs=docs)


def _swap_in(fresh: Path, dest: Path, stamp: str) -> Path | None:
    """Move `dest` (and its sidecars) aside and put `fresh` in its place."""
    aside = dest.with_name(f"{dest.name}.pre-restore-{stamp}")
    moved: list[tuple[Path, Path]] = []
    previous: Path | None = None
    try:
        if dest.exists():
            os.replace(dest, aside)
            moved.append((aside, dest))
            previous = aside
        for suffix in _SIDECARS:
            side = Path(f"{dest}{suffix}")
            if side.exists():
                side_aside = Path(f"{aside}{suffix}")
                os.replace(side, side_aside)
                moved.append((side_aside, side))
        os.replace(fresh, dest)
    except OSError as exc:
        for current, original in reversed(moved):  # best effort: put the old files back
            try:
                os.replace(current, original)
            except OSError:
                log.error("could not put back %s", original)
        raise BackupError(f"could not swap in the restored database: {exc}") from exc
    return previous
