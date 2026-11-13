# Phase 26: Data safety and local test gaps

**Days:** Fri Nov 13 · **Depends on:** 25 · **Where:** local only (no AWS calls, no deploy) · **Source:** `docs/FINAL_PROJECT_ROADMAP.md`, finding 1, items T2 to T8 and S7, failure scenarios table 6.6

## Goal

No command can lose the corpus, and every phase 25 behavior has a committed test.

## Read first

- `docs/FINAL_PROJECT_ROADMAP.md`, sections 5 (finding 1), 6.1 and 6.6
- `docs/RECOVERY.md`
- `infra/up.sh` (`save_snapshot`), `infra/restore-test-db.sh` (the embedded check), `kb/portable.py` (`claim_scheduled_export`)

## Tasks

1. **`infra/up.sh save` (T6, S7, P0).** Before anything else, copy the current laptop copy `kb-latest.db.gz` into `saved/` (unless an identical copy is already there). Download to a temporary file and check it: gzip readable, `PRAGMA integrity_check` is `ok`, the `docs` and `chunks` tables exist, and the document count is at least the current copy's. On any failure keep the old file byte-identical, delete the temporary file and exit 1. `--force` accepts a smaller (but intact) copy; a corrupt one is never accepted. The check is a standard-library Python script, `infra/scripts/check_sqlite_copy.py`, so the laptop needs no virtualenv for it.
2. **Export claim race (T2).** A `pg` test: several threads with a barrier call `claim_scheduled_export` at the same moment; exactly one gets the claim.
3. **Restore check (T3).** Move the Python embedded in `infra/restore-test-db.sh` to `infra/scripts/verify_restore.py` (host, port, user, database, `sslmode` and the password file as arguments). Test it against the local PostgreSQL: a good copy passes, an empty database fails, a database without the `vector` extension fails.
4. **`/livez` under load (T4).** `/livez` answers in under 200 ms while every worker thread is busy (the `/healthz` test, copied).
5. **Alarm metric names (T5).** Every alarm and dashboard metric in `infra/app.yaml` uses a namespace, metric name and dimension set found in a table of the documented AWS metrics for `AWS/ApplicationELB`, `AWS/AutoScaling`, `AWS/EC2` and `AWS/RDS`.
6. **PostgreSQL to SQLite switch back (T7).** A `pg` test: data exported from PostgreSQL with `export-sqlite` is restored by the SQLite boot path (`kb/backup.py`) and opens as a working SQLite database with the same documents, chunks and vectors.
7. **Missing `config/database-url` (T8).** With `KB_DATABASE_URL_SOURCE=s3` and no object at the key, startup fails with a clear message and creates no database.
8. **Claim window (doc).** One sentence in `docs/RECOVERY.md`: an export killed after it claimed blocks the scheduled export until the window (one hour) passes; a manual export is never blocked.

## Out of scope

Everything planned after phase 26. Changes to `infra/teardown-db.sh` (it has its own working check). Any AWS call. The real `~/.config/kb-mcp` is never touched: tests use temporary directories.

## Acceptance

- [x] `up.sh save` tests: a corrupt download, a non-gzip download, a download without the `docs` table and a smaller download each leave the old file byte-identical and a copy of it in `saved/`; `--force` accepts the smaller intact copy and still refuses a corrupt one; a good larger download replaces the file.
- [x] Each new test fails with its fix reverted (shown for the save fix; the others are tests of existing behavior and must pass against the current code).
- [x] `pytest -q` passes; `KB_TEST_DATABASE_URL=... pytest -q -m "not models"` passes (PostgreSQL legs run).
- [x] `ruff check`, `cfn-lint infra/*.yaml` and `bash -n infra/*.sh` clean.
- [x] Checksum of `~/.config/kb-mcp/kb-latest.db.gz` (if present) unchanged.
- [x] `docs/RECOVERY.md`, `docs/FINAL_PROJECT_ROADMAP.md` and `docs/PROGRESS.md` updated; commit `phase 26: data safety and local test gaps`.
