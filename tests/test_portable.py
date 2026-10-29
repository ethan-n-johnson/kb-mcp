"""Moving a database between PostgreSQL and SQLite: `kb.portable`, the CLI commands, the admin export.

The round trip and the import tests need PostgreSQL (`pg_store`, skipped without
`KB_TEST_DATABASE_URL`); the SQLite-only checks always run.
"""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import pytest
from moto import mock_aws

from kb import backup, cli, db, portable, service
from kb.config import Settings
from kb.schemas import UploadRequest
from kb.search import indexing
from kb.store import Store
from tests.conftest import AUTH, OpenApp
from tests.corpus import DOCS
from tests.fakes import FakeEmbedder, FakeReranker

TABLE_ORDER = {
    "docs": "id",
    "chunks": "id",
    "doc_tags": "doc_id, tag",
    "gaps": "id",
    "audit_log": "id",
}


def build_sqlite(path: Path, *, vectors: bool = True) -> Path:
    """A SQLite kb database made through the service with the fake models.

    Five docs with tags, a verified doc, a resolved gap and an open gap, so every
    table has rows and every foreign key is used.
    """
    settings = Settings(db_path=str(path), dup_threshold=1.5)
    conn = db.connect(path)
    db.init_schema(conn)
    state = indexing.build_state(settings, FakeEmbedder(), FakeReranker())
    asyncio.run(indexing.start(state))
    ids = {}
    for key, (md, tags) in DOCS.items():
        result = service.upload(
            conn, UploadRequest(content=md, tags=tags), "seed", search_state=state if vectors else None
        )
        ids[key] = result.doc_id
    service.verify(conn, ids["s3"], "alice", note="checked")
    conn.execute(
        "INSERT INTO gaps (query, query_norm, top_score, mode, created_at, resolved_doc_id)"
        " VALUES ('how to tune kafka', 'tune kafka', 0.0125, 'semantic', '2026-10-01T10:00:00Z', NULL),"
        "        ('what is a nat gateway', 'nat gateway', 0.8125, 'keyword', '2026-10-02T10:00:00Z', ?)",
        (ids["vpc"],),
    )
    conn.close()
    return path


def table_rows(path: Path | str, table: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {TABLE_ORDER[table]}")]
    finally:
        conn.close()


pg = pytest.mark.pg


@pg
def test_round_trip_sqlite_to_postgres_and_back_is_identical(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    imported = portable.import_sqlite(pg_store, source)
    exported = tmp_path / "back.db"
    portable.export_sqlite(pg_store, exported)

    for table in TABLE_ORDER:
        before, after = table_rows(source, table), table_rows(exported, table)
        assert before, f"{table} should have rows in the source"
        assert before == after, f"{table} differs after the round trip"
        assert imported.counts[table] == len(before)
    chunks = table_rows(source, "chunks")
    assert all(row[5] is not None and row[6] == "fake-bow-384" for row in chunks)  # vectors travelled


@pg
def test_the_exported_file_is_a_working_database(pg_store: Store, tmp_path: Path) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "source.db"))
    exported = tmp_path / "back.db"
    portable.export_sqlite(pg_store, exported)
    conn = db.connect(exported)
    try:
        assert conn.fetchone("PRAGMA integrity_check")[0] == "ok"
        assert conn.fts_rows(["balancer"], [], 5, with_snippet=False)  # FTS5 was built by the triggers
        assert conn.fetchone("PRAGMA journal_mode")[0] == "delete"
    finally:
        conn.close()
    assert backup._check_database(exported) == len(DOCS)  # restorable like any snapshot


@pg
def test_import_keeps_ids_and_moves_the_sequences(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    # remove a doc so ids are not 1..n (a gap in the sequence must be kept)
    conn = db.connect(source)
    conn.execute("DELETE FROM docs WHERE id = 2")
    conn.close()
    portable.import_sqlite(pg_store, source)
    assert [r["id"] for r in pg_store.fetchall("SELECT id FROM docs ORDER BY id")] == [1, 3, 4, 5]
    new = service.upload(pg_store, UploadRequest(content="# New\n\n" + "word " * 60), "t")
    assert new.doc_id == 6
    last_audit = pg_store.fetchone("SELECT max(id) AS m FROM audit_log")["m"]
    assert pg_store.fetchone("SELECT id FROM audit_log WHERE action = 'upload' ORDER BY id DESC")["id"] == last_audit


@pg
def test_import_refuses_a_database_with_documents_unless_forced(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    portable.import_sqlite(pg_store, source)
    with pytest.raises(portable.DatabaseNotEmpty):
        portable.import_sqlite(pg_store, source)
    assert pg_store.fetchone("SELECT count(*) AS n FROM docs")["n"] == len(DOCS)

    other = tmp_path / "other.db"
    conn = db.connect(other)
    db.init_schema(conn)
    service.upload(conn, UploadRequest(content="# Only\n\n" + "text " * 60), "t")
    conn.close()
    portable.import_sqlite(pg_store, other, force=True)
    assert [r["title"] for r in pg_store.fetchall("SELECT title FROM docs")] == ["Only"]
    assert pg_store.fetchone("SELECT count(*) AS n FROM audit_log")["n"] == 1  # the old rows are gone


@pg
def test_a_failed_import_leaves_the_database_empty(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    conn = sqlite3.connect(source)  # foreign keys are off on a plain connection
    conn.execute("UPDATE chunks SET doc_id = 999 WHERE id = 3")  # a chunk whose doc does not exist
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        portable.import_sqlite(pg_store, source)
    assert pg_store.fetchone("SELECT count(*) AS n FROM docs")["n"] == 0  # one transaction: nothing stayed


@pg
def test_embeddings_of_the_wrong_size_are_dropped_not_fatal(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    conn = sqlite3.connect(source)
    conn.execute("UPDATE chunks SET embedding = ? WHERE id = 1", (np.zeros(10, dtype=np.float32).tobytes(),))
    conn.commit()
    conn.close()
    result = portable.import_sqlite(pg_store, source)
    assert result.dropped_vectors == 1
    row = pg_store.fetchone("SELECT embedding, embed_model FROM chunks WHERE id = 1")
    assert row["embedding"] is None and row["embed_model"] is None  # the backfill embeds it


@pg
def test_a_source_without_vectors_imports_with_null_embeddings(pg_store: Store, tmp_path: Path) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "source.db", vectors=False))
    assert pg_store.fetchone("SELECT count(*) AS n FROM chunks WHERE embedding IS NOT NULL")["n"] == 0


@pg
def test_an_old_file_without_the_audit_note_column_imports(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE audit_log DROP COLUMN note")
    conn.commit()
    conn.close()
    portable.import_sqlite(pg_store, source)
    assert pg_store.fetchone("SELECT count(*) AS n FROM audit_log WHERE note IS NOT NULL")["n"] == 0


def test_import_needs_a_postgres_store(conn: Store, tmp_path: Path) -> None:
    if conn.dialect == "postgres":  # pragma: no cover
        pytest.skip("SQLite only")
    with pytest.raises(ValueError):
        portable.import_sqlite(conn, build_sqlite(tmp_path / "source.db"))


# --- S3 front ends ----------------------------------------------------------------------


@pg
@mock_aws
def test_export_to_s3_writes_a_restorable_snapshot(pg_store: Store, tmp_path: Path) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "source.db"))
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    result = portable.export_to(pg_store, "s3://kb-x/snapshots/kb-latest.db.gz")
    assert result.uploaded == "s3://kb-x/snapshots/kb-latest.db.gz"
    body = s3.get_object(Bucket="kb-x", Key="snapshots/kb-latest.db.gz")["Body"].read()
    assert gzip.decompress(body).startswith(b"SQLite format 3")
    assert s3.head_object(Bucket="kb-x", Key="snapshots/kb-latest.db.gz")["ServerSideEncryption"] == "AES256"
    fetched, docs = backup.fetch_database(s3, "kb-x", "snapshots/kb-latest.db.gz", tmp_path)
    assert docs == len(DOCS)
    assert table_rows(fetched, "docs") == table_rows(tmp_path / "source.db", "docs")


@pg
@mock_aws
def test_import_from_s3_key_and_address(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    backup.create_snapshot(source, "kb-x")
    result = portable.import_from(pg_store, "snapshots/kb-latest.db.gz", bucket="kb-x")
    assert result.counts["docs"] == len(DOCS)
    again = portable.import_from(pg_store, "s3://kb-x/snapshots/kb-latest.db.gz", force=True)
    assert again.counts["chunks"] == result.counts["chunks"]


@pg
@mock_aws
def test_a_missing_snapshot_is_nothing_to_import_not_an_error(pg_store: Store) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="kb-x")
    with pytest.raises(portable.NothingToImport):
        portable.import_from(pg_store, "snapshots/kb-latest.db.gz", bucket="kb-x")


@pg
def test_import_from_a_gzip_file(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    packed = tmp_path / "source.db.gz"
    packed.write_bytes(gzip.compress(source.read_bytes()))
    assert portable.import_from(pg_store, str(packed)).counts["docs"] == len(DOCS)


# --- CLI ----------------------------------------------------------------------------------


@pg
def test_cli_import_then_if_empty_skips_then_export(
    pg_store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = build_sqlite(tmp_path / "source.db")
    assert cli.main(["import-sqlite", "--from", str(source)]) == 0
    out = capsys.readouterr().out
    assert f"{len(DOCS)} docs" in out

    assert cli.main(["import-sqlite", "--from", str(source), "--if-empty"]) == 0
    assert "skipped" in capsys.readouterr().out
    assert cli.main(["import-sqlite", "--from", str(source)]) == 1
    assert "--force" in capsys.readouterr().err
    assert cli.main(["import-sqlite", "--from", str(source), "--force"]) == 0

    dest = tmp_path / "out" / "export.db"
    assert cli.main(["export-sqlite", "--to", str(dest)]) == 0
    assert table_rows(dest, "docs") == table_rows(source, "docs")


@pg
@mock_aws
def test_cli_a_missing_snapshot_exits_zero_and_other_s3_trouble_exits_one(
    pg_store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="kb-x")
    monkeypatch.setenv("KB_BUCKET", "kb-x")
    assert cli.main(["import-sqlite", "--from", "snapshots/kb-latest.db.gz", "--if-empty"]) == 0
    assert "nothing to import" in capsys.readouterr().out
    monkeypatch.setenv("KB_BUCKET", "no-such-bucket")
    assert cli.main(["import-sqlite", "--from", "snapshots/kb-latest.db.gz", "--if-empty"]) == 1
    assert "import failed" in capsys.readouterr().err


@pg
@mock_aws
def test_cli_export_defaults_to_the_snapshot_key(
    pg_store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "source.db"))
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    monkeypatch.setenv("KB_BUCKET", "kb-x")
    assert cli.main(["export-sqlite"]) == 0
    assert "s3://kb-x/snapshots/kb-latest.db.gz" in capsys.readouterr().out
    assert s3.head_object(Bucket="kb-x", Key="snapshots/kb-latest.db.gz")["ContentLength"] > 0


def test_cli_postgres_commands_need_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["import-sqlite", "--from", "x.db"]) == 1
    assert "KB_DATABASE_URL" in capsys.readouterr().err
    assert cli.main(["export-sqlite", "--to", "x.db"]) == 1
    assert "KB_DATABASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize("command", [["backup"], ["restore", "--latest", "--force"], ["snapshot-push"]])
def test_sqlite_file_commands_refuse_in_postgres_mode(
    command: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KB_DATABASE_URL", "postgresql://u:secret@localhost:1/none")
    assert cli.main(command) == 1
    err = capsys.readouterr().err
    assert "KB_DATABASE_URL is set" in err and "secret" not in err


# --- POST /api/admin/export -----------------------------------------------------------------

@pytest.mark.dual
@mock_aws
def test_admin_export_needs_a_token_and_postgres(open_app: OpenApp, backend: str) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="kb-x")
    with open_app(models=False, KB_BUCKET="kb-x", KB_SNAPSHOTS="off") as client:
        assert client.post("/api/admin/export").status_code == 401
        resp = client.post("/api/admin/export", headers=AUTH)
        if backend == "sqlite":
            assert resp.status_code == 400 and resp.json()["error"] == "bad_request"
            return
        assert resp.status_code == 200
        assert resp.json() == {"status": "exported", "key": "snapshots/kb-latest.db.gz", "docs": 0, "chunks": 0}


@pg
@mock_aws
def test_admin_export_contains_a_fresh_write_and_is_audited(
    open_app: OpenApp, pg_store: Store, tmp_path: Path
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    with open_app(models=False, KB_BUCKET="kb-x") as client:
        created = client.post(
            "/api/docs", json={"content": "# Fresh\n\n" + "words " * 60, "format": "markdown"}, headers=AUTH
        )
        assert created.status_code == 201
        resp = client.post("/api/admin/export", headers=AUTH)
        assert resp.status_code == 200 and resp.json()["docs"] == 1
        audit = client.get("/api/audit").json()["entries"]
    assert [(e["token_name"], e["action"]) for e in audit][:2] == [("test", "export"), ("test", "upload")]
    fetched, docs = backup.fetch_database(s3, "kb-x", "snapshots/kb-latest.db.gz", tmp_path)
    assert docs == 1
    assert [r[1] for r in table_rows(fetched, "docs")] == ["Fresh"]


@pg
def test_admin_export_without_a_bucket_is_a_400(open_app: OpenApp, pg_store: Store) -> None:
    with open_app(models=False) as client:
        resp = client.post("/api/admin/export", headers=AUTH)
    assert resp.status_code == 400 and "KB_BUCKET" in resp.json()["detail"]


@pg
def test_ids_deleted_from_the_top_are_not_reused_in_either_direction(pg_store: Store, tmp_path: Path) -> None:
    source = build_sqlite(tmp_path / "source.db")
    conn = db.connect(source)
    conn.execute("DELETE FROM docs WHERE id = 5")  # SQLite AUTOINCREMENT remembers 5
    conn.close()
    portable.import_sqlite(pg_store, source)
    new = service.upload(pg_store, UploadRequest(content="# New\n\n" + "word " * 60), "t")
    assert new.doc_id == 6  # not 5
    # and the export remembers it for the next import
    out = tmp_path / "back.db"
    portable.export_sqlite(pg_store, out)
    conn = sqlite3.connect(out)
    assert dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))["docs"] == 6
    conn.close()
    service.delete(pg_store, new.doc_id, "t")
    portable.export_sqlite(pg_store, out)  # 6 is gone, the sequence still says 6
    conn = sqlite3.connect(out)
    assert dict(conn.execute("SELECT name, seq FROM sqlite_sequence"))["docs"] == 6
    conn.close()
    portable.import_sqlite(pg_store, out, force=True)
    again = service.upload(pg_store, UploadRequest(content="# Again\n\n" + "text " * 60), "t")
    assert again.doc_id == 7
