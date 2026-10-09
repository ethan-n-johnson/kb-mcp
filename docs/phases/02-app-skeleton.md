# Phase 02: App skeleton and schema

**Day:** 1 (Wed Oct 7) · **Depends on:** 00 · **Where:** local · **PLAN.md sections:** Architecture (code layout, runtime rules); Ingestion and data model (SQLite schema)

## Goal

A runnable FastAPI app with the full package layout, configuration, the complete SQLite schema with FTS5 triggers, JSON logging, `/healthz`, and a test harness. Later phases fill in the empty modules.

## Read first

- `CLAUDE.md` (runtime rules and config table)
- `docs/PLAN.md`: "Architecture" and the schema table under "Ingestion and data model"

## Tasks

1. `pyproject.toml`: package `kb`, Python `>=3.12`. Runtime deps: fastapi, uvicorn[standard], mcp, fastembed, numpy, trafilatura>=2.0, pymupdf4llm, httpx, jinja2, nh3, slowapi, boto3, pyyaml, markdown-it-py, python-multipart. Dev extra: pytest, moto[s3,ssm], cfn-lint. Pytest config: register marker `models`, `addopts = "-m 'not models'"`.
2. Create every module from PLAN.md's layout (`kb/main.py`, `config.py`, `db.py`, `service.py`, `ingest/*`, `search/*`, `metrics.py`, `mcp_tools.py`, `api.py`, `auth.py`, `cli.py`, `templates/`, plus `kb/schemas.py` for shared Pydantic models and `kb/logging.py`). Empty modules get a one-line docstring naming the phase that fills them.
3. `kb/config.py`: a frozen settings object built from the environment, with the defaults in `CLAUDE.md`. Function `load_write_tokens() -> dict[str, str]`: parse `KB_WRITE_TOKENS` if set; otherwise read the SSM SecureString (`with_decryption=True`) once and cache it. Never log the values; log only the token names.
4. `kb/logging.py`: configure the root logger to emit one JSON object per line (`ts`, `level`, `logger`, `msg`, plus any `extra` fields). Function `setup_logging(stream)` so stdio mode can pass `sys.stderr`.
5. `kb/db.py`:
   - `connect(path)` sets `journal_mode=DELETE`, `foreign_keys=ON`, `busy_timeout=5000`, `row_factory=sqlite3.Row`; creates the parent directory if missing.
   - `init_schema(conn)` is idempotent (`CREATE ... IF NOT EXISTS`) and creates all six tables from PLAN.md, adding `docs.s3_key` (PLAN mentions it in the purpose column). Use `ON DELETE CASCADE` from `chunks` and `doc_tags` to `docs`. Unique index on `docs.content_hash`; indexes on `chunks.doc_id`, `doc_tags.tag`, `gaps.query_norm`. Timestamps are ISO 8601 UTC strings.
   - `chunks_fts`: `CREATE VIRTUAL TABLE chunks_fts USING fts5(heading_path, text, content='chunks', content_rowid='id', tokenize='porter unicode61')`, plus the three external-content triggers: after insert (insert row), after delete (insert the `'delete'` command row with old values), after update (the `'delete'` command row, then insert new values).
   - A `transaction(conn)` context manager (`BEGIN IMMEDIATE`, commit or rollback).
6. `kb/main.py`: FastAPI app with a lifespan that sets up logging, opens the DB, runs `init_schema`, and stores the connection and settings on `app.state`. `GET /healthz` returns `{"status": "ok", "mode": "keyword", "models": {"embedder": "off", "reranker": "off"}, "fallback_toggle": false, "chunks": <count>}` with 200 when a `SELECT count(*) FROM chunks` succeeds, 503 otherwise. Phase 07 makes `mode` and `models` real.
7. `kb/service.py`: function signatures for every operation in PLAN.md's interfaces table (`search`, `get`, `upload`, `update`, `delete`, `merge`, `ingest_url`, `list_tags`, `verify`, `stale`, `gaps`) raising `NotImplementedError`, each with a docstring stating its arguments and return shape.
8. Tests (`tests/`): `conftest.py` with a temp-DB fixture and a `TestClient` fixture (`KB_MODELS=off`, `KB_WRITE_TOKENS=test:testtoken`). `test_db.py`: schema creation twice is a no-op; `journal_mode` is `delete`; inserting, updating and deleting a chunk keeps `chunks_fts` in sync (check with a `MATCH` query); deleting a doc cascades to chunks, tags and FTS. `test_health.py`: `/healthz` returns 200 and the documented keys.

## Do not

- Implement ingestion, search, auth or MCP.
- Use WAL mode or an ORM.

## Acceptance

- [ ] `pip install -e '.[dev]'` succeeds in a fresh `.venv`.
- [ ] `pytest -q` passes.
- [ ] Running the local command in `CLAUDE.md`, `curl -s localhost:8000/healthz` returns 200 JSON with `chunks: 0`.
- [ ] `sqlite3 .localdata/kb.db 'pragma journal_mode'` prints `delete`.
- [ ] Logs on stdout are valid JSON lines (`... | head -3 | python -m json.tool --json-lines`).
- [ ] Phase log updated; commit `phase 02: app skeleton and schema`.
