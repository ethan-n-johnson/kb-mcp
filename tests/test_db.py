"""Schema, pragmas, FTS5 trigger sync and cascades."""

from __future__ import annotations

import sqlite3

import pytest

from kb import db

NOW = "2026-10-07T00:00:00Z"


def _add_doc(conn: sqlite3.Connection, hash_: str = "h1") -> int:
    cur = conn.execute(
        "INSERT INTO docs(title, source_type, content_md, content_hash, created_at, updated_at)"
        " VALUES ('T', 'markdown', 'body', ?, ?, ?)",
        (hash_, NOW, NOW),
    )
    return int(cur.lastrowid)


def _add_chunk(conn: sqlite3.Connection, doc_id: int, text: str, heading: str = "H") -> int:
    cur = conn.execute(
        "INSERT INTO chunks(doc_id, ord, heading_path, text) VALUES (?, 0, ?, ?)",
        (doc_id, heading, text),
    )
    return int(cur.lastrowid)


def _match(conn: sqlite3.Connection, term: str) -> list[int]:
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rowid", (term,)
    ).fetchall()
    return [r[0] for r in rows]


def _objects(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


def test_all_tables_present(conn: sqlite3.Connection) -> None:
    names = _objects(conn)
    for table in ("docs", "chunks", "chunks_fts", "doc_tags", "gaps", "audit_log"):
        assert table in names
    for trig in ("chunks_ai", "chunks_ad", "chunks_au"):
        assert trig in names


def test_init_schema_is_idempotent(conn: sqlite3.Connection) -> None:
    before = _objects(conn)
    db.init_schema(conn)
    db.init_schema(conn)
    assert _objects(conn) == before


def test_pragmas(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.row_factory is sqlite3.Row


def test_connect_creates_parent_dir(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "kb.db"
    c = db.connect(target)
    c.close()
    assert target.exists()


def test_content_hash_is_unique(conn: sqlite3.Connection) -> None:
    _add_doc(conn, "same")
    with pytest.raises(sqlite3.IntegrityError):
        _add_doc(conn, "same")


def test_fts_follows_insert_update_delete(conn: sqlite3.Connection) -> None:
    doc = _add_doc(conn)
    cid = _add_chunk(conn, doc, "attaching volumes to instances", "EBS > Attach")
    assert _match(conn, "volume") == [cid]  # porter stemming: volumes -> volume
    assert _match(conn, "EBS") == [cid]  # heading_path is indexed

    conn.execute("UPDATE chunks SET text = 'snapshots of buckets' WHERE id = ?", (cid,))
    assert _match(conn, "volume") == []
    assert _match(conn, "snapshot") == [cid]

    conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
    assert _match(conn, "snapshot") == []
    assert _match(conn, "EBS") == []


def test_delete_doc_cascades(conn: sqlite3.Connection) -> None:
    doc = _add_doc(conn)
    other = _add_doc(conn, "h2")
    _add_chunk(conn, doc, "elastic block store")
    keep = _add_chunk(conn, other, "elastic load balancer")
    conn.execute("INSERT INTO doc_tags(doc_id, tag) VALUES (?, 'ebs')", (doc,))
    conn.execute("INSERT INTO doc_tags(doc_id, tag) VALUES (?, 'elb')", (other,))
    assert _match(conn, "elastic") != []

    conn.execute("DELETE FROM docs WHERE id = ?", (doc,))

    assert conn.execute("SELECT count(*) FROM chunks WHERE doc_id = ?", (doc,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM doc_tags WHERE doc_id = ?", (doc,)).fetchone()[0] == 0
    assert _match(conn, "block") == []
    assert _match(conn, "elastic") == [keep]
    assert conn.execute("SELECT count(*) FROM doc_tags").fetchone()[0] == 1


def test_gap_resolution_survives_doc_delete(conn: sqlite3.Connection) -> None:
    doc = _add_doc(conn)
    conn.execute(
        "INSERT INTO gaps(query, query_norm, created_at, resolved_doc_id) VALUES ('q', 'q', ?, ?)",
        (NOW, doc),
    )
    conn.execute("DELETE FROM docs WHERE id = ?", (doc,))
    assert conn.execute("SELECT resolved_doc_id FROM gaps").fetchone()[0] is None


def test_transaction_commits_and_rolls_back(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        _add_doc(conn, "ok")
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 1

    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            _add_doc(conn, "rolled-back")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 1
    assert not conn.in_transaction


def test_audit_log_has_a_note_column(conn: sqlite3.Connection) -> None:
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")]
    assert columns == ["id", "token_name", "action", "doc_id", "at", "note"]


def test_an_older_audit_table_is_upgraded_in_place(tmp_path) -> None:
    """A database created before phase 08 has no `note` column; opening it adds one."""
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.executescript(db.SCHEMA.replace(",\n    note        TEXT", ""))
    old.execute("INSERT INTO audit_log (token_name, action, doc_id, at) VALUES ('t', 'upload', 1, ?)", (NOW,))
    assert "note" not in [r["name"] for r in old.execute("PRAGMA table_info(audit_log)")]
    db.init_schema(old)
    db.init_schema(old)  # still idempotent
    row = old.execute("SELECT token_name, action, note FROM audit_log").fetchone()
    old.close()
    assert tuple(row) == ("t", "upload", None)
    again = db.connect(path)
    assert "note" in [r["name"] for r in again.execute("PRAGMA table_info(audit_log)")]
    again.close()
