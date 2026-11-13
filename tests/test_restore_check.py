"""infra/scripts/verify_restore.py, the check `infra/restore-test-db.sh` runs on a restored copy (phase 26, T3).

Runs the script as `restore-test-db.sh` does (a subprocess with `-I`) against the
test PostgreSQL server: the good copy is the test database after an import, the
other cases are scratch databases created and dropped here. Skipped without
`KB_TEST_DATABASE_URL`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import psycopg
import pytest

from kb import portable
from kb.store import Store
from tests.test_portable import build_sqlite

pytestmark = pytest.mark.pg

SCRIPT = Path(__file__).resolve().parent.parent / "infra" / "scripts" / "verify_restore.py"
SCRATCH = "kb_restore_check_scratch"


def run_check(tmp_path: Path, dbname: str | None = None, password: str | None = None) -> subprocess.CompletedProcess[str]:
    url = urlsplit(os.environ["KB_TEST_DATABASE_URL"])
    secret = tmp_path / "db-password"
    secret.write_text((url.password or "" if password is None else password) + "\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable, "-I", str(SCRIPT),
            "--host", url.hostname or "localhost", "--port", str(url.port or 5432),
            "--user", url.username or "postgres", "--dbname", dbname or url.path.lstrip("/"),
            "--sslmode", "disable", "--password-file", str(secret),
        ],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def scratch_db(pg_store: Store) -> Iterator[str]:
    """An empty database (no tables, no extensions) next to the test database."""
    admin = os.environ["KB_TEST_DATABASE_URL"]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        conn.execute(f"CREATE DATABASE {SCRATCH}")
    try:
        yield SCRATCH
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")


def test_a_good_copy_passes(pg_store: Store, tmp_path: Path) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "src.db"))
    result = run_check(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTORE CHECK PASSED" in result.stdout
    assert "5 docs" in result.stdout and "pgvector" in result.stdout


def test_a_copy_with_the_schema_but_no_documents_fails(pg_store: Store, tmp_path: Path) -> None:
    result = run_check(tmp_path)
    assert result.returncode == 1
    assert "the copy is empty" in result.stdout and "RESTORE CHECK FAILED" in result.stdout


def test_a_copy_with_unembedded_chunks_fails(pg_store: Store, tmp_path: Path) -> None:
    portable.import_sqlite(pg_store, build_sqlite(tmp_path / "src.db", vectors=False))
    result = run_check(tmp_path)
    assert result.returncode == 1 and "some chunks have no embedding" in result.stdout


def test_an_empty_database_fails_cleanly(scratch_db: str, tmp_path: Path) -> None:
    result = run_check(tmp_path, dbname=scratch_db)
    assert result.returncode == 1 and "RESTORE CHECK FAILED" in result.stdout
    assert "pgvector extension is not installed" in result.stdout
    assert "Traceback" not in result.stderr


def test_a_database_without_the_vector_extension_fails(scratch_db: str, tmp_path: Path) -> None:
    """Tables that look right, but no pgvector: search could not run on this copy."""
    url = os.environ["KB_TEST_DATABASE_URL"].rsplit("/", 1)[0] + f"/{scratch_db}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE TABLE docs (id int, updated_at text)")
        conn.execute("CREATE TABLE chunks (id int, embedding bytea)")
        conn.execute("INSERT INTO docs VALUES (1, '2026-11-13')")
        conn.execute("INSERT INTO chunks VALUES (1, '\\x00')")
    result = run_check(tmp_path, dbname=scratch_db)
    assert result.returncode == 1 and "pgvector extension is not installed" in result.stdout


def test_a_wrong_password_fails_without_printing_it(pg_store: Store, tmp_path: Path) -> None:
    result = run_check(tmp_path, password="Wr0ngPassw0rdXyz")
    assert result.returncode == 1 and "RESTORE CHECK FAILED" in result.stdout
    assert "Wr0ngPassw0rdXyz" not in result.stdout + result.stderr
