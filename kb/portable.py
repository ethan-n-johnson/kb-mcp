"""Move a kb database between PostgreSQL and SQLite files.

`export_sqlite` writes the PostgreSQL contents into a fresh SQLite file (ids and
embeddings included) and `import_sqlite` copies a SQLite file into an empty
PostgreSQL database, keeping ids and not re-embedding anything. The SQLite file
is the portable format: it is what `infra/up.sh save` downloads, what a new
empty sandbox account restores from, and what the app imports at boot in
PostgreSQL mode (`snapshots/kb-latest.db.gz`).

Both directions copy the same five tables in dependency order and compare equal
row for row: timestamps are the same ISO 8601 UTC strings, embeddings the same
float32 bytes (`tests/test_portable.py`).
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb import backup, db
from kb.search.index import DIM
from kb.store.base import Store

log = logging.getLogger(__name__)

BATCH = 500

# Tables in dependency order (a table follows the ones it references) with their
# columns. Tables with a serial `id` are copied in id order and their sequences
# are reset after an import.
COLUMNS: dict[str, list[str]] = {
    "docs": [
        "id", "title", "source_url", "source_type", "content_md", "content_hash",
        "s3_key", "created_at", "updated_at", "verified_at", "verified_by",
    ],
    "chunks": ["id", "doc_id", "ord", "heading_path", "text", "embedding", "embed_model"],
    "doc_tags": ["doc_id", "tag"],
    "gaps": ["id", "query", "query_norm", "top_score", "mode", "created_at", "resolved_doc_id"],
    "audit_log": ["id", "token_name", "action", "doc_id", "at", "note"],
}
HAS_ID = ("docs", "chunks", "gaps", "audit_log")


class DatabaseNotEmpty(Exception):
    """The PostgreSQL database already has documents and `force` was not given."""


class NothingToImport(Exception):
    """The source object does not exist (a brand new deployment)."""


@dataclass
class TransferResult:
    """Row counts per table, plus embeddings dropped because of a wrong size."""

    counts: dict[str, int] = field(default_factory=dict)
    dropped_vectors: int = 0
    uploaded: str | None = None


def _copy_rows(sql_rows: Any, table: str, dest: Store, cols: list[str]) -> int:
    """Insert `sql_rows` (an iterable of tuples in `cols` order) into `dest` in batches."""
    insert = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    total, batch = 0, []
    for row in sql_rows:
        batch.append(tuple(row))
        if len(batch) >= BATCH:
            dest.executemany(insert, batch)
            total += len(batch)
            batch = []
    if batch:
        dest.executemany(insert, batch)
        total += len(batch)
    return total


def _paged(store: Store, table: str, cols: list[str]) -> Any:
    """Rows of `table` in primary key order, fetched in pages of `BATCH`."""
    select = ", ".join(cols)
    if table not in HAS_ID:
        yield from store.fetchall(f"SELECT {select} FROM {table} ORDER BY doc_id, tag")
        return
    last = 0
    while True:
        rows = store.fetchall(
            f"SELECT {select} FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (last, BATCH)
        )
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]


def export_sqlite(store: Store, dest: str | Path) -> TransferResult:
    """Write the contents of `store` into a fresh SQLite file at `dest` (replaced if present).

    The file gets the normal SQLite schema, so the FTS5 index is built by the
    triggers as the chunks are inserted, and the result can be restored like any
    snapshot.
    """
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (path, Path(f"{path}-journal")):
        stale.unlink(missing_ok=True)
    out = db.connect(path)
    result = TransferResult()
    try:
        db.init_schema(out)
        with db.transaction(out):
            for table, cols in COLUMNS.items():
                result.counts[table] = _copy_rows(_paged(store, table, cols), table, out, cols)
            _record_sequences(store, out)
    finally:
        out.close()
    log.info("exported to sqlite", extra={"counts": result.counts, "path": str(path)})
    return result


def _source_columns(src: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in src.execute(f"PRAGMA table_info({table})")}


def _source_rows(src: sqlite3.Connection, table: str, cols: list[str], dropped: list[int]) -> Any:
    """Rows of the SQLite `table`, columns missing in an old file as NULL, bad vectors dropped."""
    have = _source_columns(src, table)
    select = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
    order = "ORDER BY id" if table in HAS_ID else "ORDER BY doc_id, tag"
    cursor = src.execute(f"SELECT {select} FROM {table} {order}")
    if table != "chunks":
        yield from cursor
        return
    emb, model = cols.index("embedding"), cols.index("embed_model")
    for row in cursor:
        if row[emb] is not None and len(row[emb]) != DIM * 4:
            # Not re-embedded here: the startup backfill embeds chunks without a vector.
            row = list(row)
            row[emb], row[model] = None, None
            dropped[0] += 1
        yield row


def _source_sequences(src: sqlite3.Connection) -> dict[str, int]:
    """Last id ever handed out per table (`sqlite_sequence`), which also counts deleted rows."""
    has = src.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone()
    if has is None:
        return {}
    return {name: int(seq) for name, seq in src.execute("SELECT name, seq FROM sqlite_sequence")}


def _reset_sequences(store: Store, last_ids: dict[str, int]) -> None:
    """Move each id sequence past the largest imported id and past every id the source ever used.

    SQLite's AUTOINCREMENT never reuses an id, and `audit_log.doc_id` has no foreign key
    that would stop an old row from naming a deleted doc, so a new doc must not take the
    id of one deleted from the top.
    """
    for table in HAS_ID:
        floor = int(last_ids.get(table, 0))
        store.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
            f" GREATEST(COALESCE(max(id), 0), {floor}, 1), COALESCE(max(id), 0) > 0 OR {floor} > 0)"
            f" FROM {table}"
        )


def _record_sequences(store: Store, out: sqlite3.Connection) -> None:
    """Write PostgreSQL's last-used ids into the SQLite file's `sqlite_sequence`."""
    for table in HAS_ID:
        seq = store.fetchone(f"SELECT pg_get_serial_sequence('{table}', 'id') AS name")["name"]
        state = store.fetchone(f"SELECT last_value, is_called FROM {seq}")
        last = int(state["last_value"]) if state["is_called"] else 0
        last = max(last, int(out.execute(f"SELECT COALESCE(max(id), 0) FROM {table}").fetchone()[0]))
        out.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        out.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, last))


def import_sqlite(store: Store, src_path: str | Path, *, force: bool = False) -> TransferResult:
    """Copy a SQLite kb database into the (empty) PostgreSQL database `store`, in one transaction.

    Ids, timestamps, tags, gaps, the audit log and the stored embeddings are kept
    as they are; nothing is embedded. Then the id sequences are moved past the
    largest imported id. Raises `DatabaseNotEmpty` if `store` already has
    documents, unless `force`, which empties every table first.
    """
    if store.dialect != "postgres":
        raise ValueError("import-sqlite writes into PostgreSQL; the store is SQLite")
    path = Path(src_path)
    src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    result = TransferResult()
    dropped = [0]
    try:
        with db.transaction(store):
            existing = int(store.fetchone("SELECT count(*) FROM docs")[0])
            if existing and not force:
                raise DatabaseNotEmpty(f"the database already has {existing} documents")
            if existing:
                store.execute("TRUNCATE gaps, audit_log, doc_tags, chunks, docs RESTART IDENTITY")
            for table, cols in COLUMNS.items():
                rows = _source_rows(src, table, cols, dropped)
                result.counts[table] = _copy_rows(rows, table, store, cols)
            _reset_sequences(store, _source_sequences(src))
    finally:
        src.close()
    result.dropped_vectors = dropped[0]
    if dropped[0]:
        log.warning("embeddings with the wrong size were not imported", extra={"count": dropped[0]})
    log.info("imported from sqlite", extra={"counts": result.counts})
    return result


# --- S3 and file front ends ------------------------------------------------------


def split_s3(target: str) -> tuple[str, str]:
    """`s3://bucket/key` into `(bucket, key)`."""
    rest = target[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError("an S3 address looks like s3://<bucket>/<key>")
    return bucket, key


def export_to(
    store: Store, target: str, *, region: str | None = None, client: Any | None = None
) -> TransferResult:
    """Export `store` to `target`: `s3://bucket/key` (gzip, uploaded) or a local file path."""
    with tempfile.TemporaryDirectory(prefix="kb-export-") as tmp:
        built = Path(tmp) / "kb.db"
        result = export_sqlite(store, built)
        if target.startswith("s3://"):
            bucket, key = split_s3(target)
            backup.upload_database(built, bucket, key, client=client, region=region)
            result.uploaded = target
        else:
            dest = Path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(built, dest)
            result.uploaded = str(dest)
    return result


def import_from(
    store: Store,
    source: str,
    *,
    bucket: str | None = None,
    force: bool = False,
    region: str | None = None,
    client: Any | None = None,
) -> TransferResult:
    """Import from a local `.db` file, `s3://bucket/key`, or a bare key in `bucket` (gzip).

    A missing S3 object raises `NothingToImport`; any other download problem raises
    `backup.BackupError`, so a transient S3 error never looks like an empty source.
    """
    if not source.startswith("s3://") and Path(source).is_file():
        if not source.endswith(".gz"):
            return import_sqlite(store, source, force=force)
        with tempfile.TemporaryDirectory(prefix="kb-import-") as tmp:
            plain = Path(tmp) / "kb.db"
            try:
                with gzip.open(source, "rb") as gz, plain.open("wb") as out:
                    shutil.copyfileobj(gz, out)
            except (OSError, EOFError) as exc:
                raise backup.CorruptBackup(f"{source} is not a valid gzip file: {exc}") from exc
            return import_sqlite(store, plain, force=force)
    if source.startswith("s3://"):
        bucket, key = split_s3(source)
    else:
        key = source
        if not bucket:
            raise ValueError(f"{source} is not a file and no bucket is set (KB_BUCKET)")
    s3 = backup._s3(client, region)
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            raise NothingToImport(f"s3://{bucket}/{key} does not exist") from exc
        raise backup.BackupError(f"could not look for s3://{bucket}/{key}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="kb-import-") as tmp:
        fetched, _docs = backup.fetch_database(s3, bucket, key, Path(tmp))
        return import_sqlite(store, fetched, force=force)
