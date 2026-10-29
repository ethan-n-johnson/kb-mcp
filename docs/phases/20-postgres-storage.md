# Phase 20: PostgreSQL storage backend

**Days:** Sat Oct 31 to Mon Nov 2 · **Depends on:** 19 · **Where:** local (Docker Postgres), then AWS · **PLAN.md sections:** Search; Data model

## Goal

The app runs on either SQLite (local, tests, the phase 1 stack) or PostgreSQL (the phase 2 stack), chosen by one setting. All writes, reads and reports work on PostgreSQL. Search on PostgreSQL is phase 21; in this phase it may still use the in-memory index loaded from PostgreSQL.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` ("Phase 2 database settings" from phase 19)
- `kb/db.py` (schema, connection, commit hook), `kb/service.py`, `kb/search/indexing.py`, `kb/seed.py`, `kb/backup.py`, `kb/snapshot.py`
- `tests/conftest.py` and the fixtures that open a database

## Design

- **Setting.** `KB_DATABASE_URL` (unset by default). Unset: SQLite at `KB_DB`, exactly as today. Set to `postgresql://...`: PostgreSQL. On the instances the URL is read from `s3://<bucket>/config/database-url` (`KB_DATABASE_URL_SOURCE=s3`, same pattern as `KB_TOKENS_SOURCE`).
- **One connection interface, two dialects.** New package `kb/store/`:
  - `base.py`: a small `Store` protocol with `execute(sql, params)`, `executemany`, `fetchone`, `fetchall`, `transaction()` (a context manager), and `dialect` (`"sqlite"` or `"postgres"`). Rows behave like mappings in both.
  - `sqlite.py`: wraps the current `sqlite3` connection (keeps `journal_mode=DELETE`, `foreign_keys=ON`, `busy_timeout`, and the snapshot commit hook).
  - `postgres.py`: `psycopg` 3 with `psycopg_pool.ConnectionPool` (min 1, max 5) and `dict_row`. It converts `?` placeholders to `%s` so most SQL in `service.py` stays as written.
  - Only dialect-specific SQL lives in the dialect modules: schema, upserts (`INSERT OR IGNORE` becomes `ON CONFLICT DO NOTHING`), `RETURNING`, date functions, and the FTS and vector queries (phase 21).
- **PostgreSQL schema** (`kb/store/schema_pg.sql`, applied at startup, idempotent): the same tables and columns as `kb/db.py`, with `BIGSERIAL` ids, `TIMESTAMPTZ` times, `BYTEA` hashes, `CREATE EXTENSION IF NOT EXISTS vector`, `chunks.embedding vector(384)`, and `chunks.tsv tsvector GENERATED ALWAYS AS (setweight(to_tsvector('english', coalesce(heading_path,'')),'A') || setweight(to_tsvector('english', text),'B')) STORED` with a GIN index. No triggers needed (the generated column replaces the FTS5 triggers).
- **Snapshots** only exist in SQLite mode. In PostgreSQL mode the commit hook and the snapshot thread are not started.
- **Portable export.** `python -m kb.cli export-sqlite --to s3://<bucket>/snapshots/kb-latest.db.gz` writes the PostgreSQL contents into a fresh SQLite file (embeddings included) and uploads it to the snapshot key. That keeps `infra/up.sh save` and the "new empty sandbox account" recovery working unchanged.
- **Import at boot.** `python -m kb.cli import-sqlite --from <s3 key or path>` copies docs, chunks with their embeddings, tags, gaps and the audit log into an empty PostgreSQL database in one transaction, keeping ids, then resets the sequences. It refuses a non-empty database unless `--force`. No re-embedding.

## Tasks

1. Add `psycopg[binary]` and `psycopg-pool` to the dependencies; `pgvector` (the Python package) for the vector adapter.
2. Build `kb/store/` as above and route every database call in `kb/` through it. `kb/db.py` keeps the SQLite schema and becomes the SQLite dialect's schema module.
3. Port `kb/service.py`, `kb/search/indexing.py` (loading vectors from either backend), `kb/seed.py`, `kb/evaluate.py` and `kb/backup.py` (SQLite only; PostgreSQL relies on RDS backups plus the export). Leave `kb/uploads.py` alone until phase 22.
4. `export-sqlite` and `import-sqlite` CLI commands, plus `POST /api/admin/export` (write token required, audited) that runs the export on demand so `infra/up.sh save` can ask for a fresh one before downloading.
5. `infra/up.sh save`: in PostgreSQL mode, call `POST $CF/api/admin/export` with a token from `~/.config/kb-mcp/write-tokens`, wait for the response, then download the snapshot as today.
6. User data in `infra/app.yaml` (new parameter `DatabaseMode`, `sqlite` or `postgres`, default `sqlite` until phase 22): in `postgres` mode, replace the restore container with `import-sqlite --from snapshots/kb-latest.db.gz` that only runs when the database is empty, and pass `-e KB_DATABASE_URL_SOURCE=s3`. Attach `kb-DbClientSg` to the launch template in `postgres` mode.
7. **Tests.** A `pg` marker and a `store` fixture parametrized over `sqlite` and `postgres`. The Postgres leg runs only when `KB_TEST_DATABASE_URL` is set. Locally: `docker run -d --name kb-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 pgvector/pgvector:pg16`, then `KB_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/postgres pytest -q -m "not models"`. Convert the write path, update, merge, reports, gaps, audit and CLI tests to the fixture first; search tests follow in phase 21. CI: add a `postgres` service container (`pgvector/pgvector:pg16`) to the pytest job and set `KB_TEST_DATABASE_URL`.
8. Round-trip test: seed a SQLite DB with the fakes, `import-sqlite` into Postgres, `export-sqlite` back, and compare every table row for row (embeddings byte for byte).
9. Deploy with `DatabaseMode=postgres` (`KB_DATABASE_MODE=postgres` in `up.env`). Check: the boot log shows the import with the doc count, `/healthz` is `ok` with the same chunk count as before, a write through REST and through MCP lands, and `infra/up.sh save` downloads an export that contains that write.

## Do not

- Re-embed anything during the import.
- Remove the SQLite path. It is the local and test default.
- Change search behavior yet (phase 21).

## Acceptance

- [ ] `pytest -q` passes (SQLite) and `KB_TEST_DATABASE_URL=... pytest -q -m "not models"` passes (both legs).
- [ ] CI is green with the Postgres service.
- [ ] Round-trip test passes.
- [ ] Deployed in `postgres` mode: chunk count matches the snapshot, a new doc survives `infra/up.sh save` and shows up in the downloaded file.
- [ ] Deviation recorded ("Phase 2: PostgreSQL backend"); phase log updated; commit `phase 20: PostgreSQL storage backend`.
