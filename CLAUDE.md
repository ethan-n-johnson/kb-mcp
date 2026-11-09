# kb-mcp

Knowledge-base MCP server for the Cloud Computing course (AWS Academy Cloud Architecting). One FastAPI service on AWS: semantic search with local CPU models, keyword fallback, 12 MCP tools, REST, web UI, Chrome capture extension. Demo: Wed Oct 14, 2026.

- Full design: `docs/PLAN.md` (source of truth for behavior and numbers)
- Work breakdown: `docs/phases/` (one file per phase, index in `docs/phases/README.md`)
- Progress log, stack outputs, deviations: `docs/PROGRESS.md`
- Computer and AWS credentials setup (Linux and Windows): `docs/SETUP.md`
- Backups, restore procedures, RPO and RTO: `docs/RECOVERY.md`; alarms and their runbook: `docs/ALARMS.md`

## How to work in this repo

1. Do exactly one phase per session. Start by reading the phase file, then `docs/PROGRESS.md`, then only the `docs/PLAN.md` sections the phase lists.
2. Stay inside the phase's scope. If something outside it is broken or missing, add it to "Open issues" in `docs/PROGRESS.md` instead of fixing it, unless the phase cannot pass without the fix.
3. Steps marked **HUMAN** need the user (AWS Academy console, credentials, secrets, email inboxes, Chrome, Claude Code config). Stop, tell the user exactly what to do, and wait. Never work around them.
4. If the PLAN and reality disagree (a library API differs, AWS rejects a setting), pick the smallest change that keeps the PLAN's intent, and record it under "Deviations" in `docs/PROGRESS.md`.
5. A phase is done only when every acceptance check in its file passes. Then append a row to the phase log in `docs/PROGRESS.md` and make one commit: `phase NN: <title>`.
6. Never use em dashes in docs, code comments, commit messages or UI text.
7. Never print, log, commit or echo secret values (write tokens, AWS keys). Check that a secret exists; do not display it.

## Stack

Python 3.12, FastAPI, uvicorn, official `mcp` SDK 2.x (`MCPServer`, called `FastMCP` in 1.x), fastembed, numpy, SQLite with FTS5 or PostgreSQL with pgvector (psycopg 3, chosen by `KB_DATABASE_URL`), trafilatura (>=2.0), pymupdf4llm, httpx, Jinja2 + htmx, nh3, slowapi, boto3, PyYAML. Dev: pytest, moto, cfn-lint.

Virtualenv at `.venv`; install with `pip install -e '.[dev]'`.

## Commands

- Fast tests (models off): `pytest -q` (SQLite; the PostgreSQL legs are skipped)
- Fast tests on both backends: `docker run -d --name kb-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 pgvector/pgvector:pg16`, then `KB_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/postgres pytest -q -m "not models"` (modules marked `dual` run on SQLite and PostgreSQL, tests marked `pg` only on PostgreSQL)
- Model tests (loads real ONNX models, slower): `pytest -q -m models`
- Run locally: `KB_DB=./.localdata/kb.db KB_WRITE_TOKENS=dev:devtoken uvicorn kb.main:app --port 8000 --workers 1`
- PostgreSQL mode locally: add `KB_DATABASE_URL=postgresql://postgres:dev@localhost:5432/kb` to the commands above (create the database first). Copy data between the two: `python -m kb.cli import-sqlite --from <file, s3://... or key>` (into an empty database; `--if-empty`, `--force`) and `python -m kb.cli export-sqlite --to <file or s3://...>` (default: the S3 snapshot key; `--once-per SECONDS` skips when any instance exported within that window, used by the nightly cron)
- Stdio MCP: `KB_DB=./.localdata/kb.db python -m kb.cli mcp-stdio` (local and trusted, so no token is needed; add `KB_WRITE_TOKENS=dev:devtoken KB_TOKEN=devtoken` to record writes as `dev` instead of `mcp`)
- Seed: `KB_DB=./.localdata/seed/kb.db KB_MODEL_DIR=./.localdata/models python -m kb.cli seed seeds/seeds.yaml` (`--dry-run` checks every entry without writing); then `python -m kb.cli eval tests/eval/questions.yaml` and `KB_BUCKET=<bucket> python -m kb.cli snapshot-push` (docs/SETUP.md, "Seed the demo corpus")
- Lint infra: `cfn-lint infra/*.yaml`
- Deploy a stack: `infra/deploy.sh network|data|db|app [ImageTag]` (created in phases 01, 06 and 19; `app` takes `KB_DATABASE_MODE=sqlite|postgres`, default sqlite, and optionally `KB_SEARCH_RATE` (the stack parameter `SearchRate`, default `120/minute`, per instance); `db` needs `KB_DB_PASSWORD` in the environment, which `infra/up.sh` supplies from `~/.config/kb-mcp/db-password`)
- Probe what RDS the sandbox allows: `infra/probe-rds.sh [--try-subnet-group]`

## Runtime rules (never break these)

- uvicorn runs with exactly **1 worker per instance** (each worker holds the models in memory). The Auto Scaling group is **1 instance in `sqlite` mode** (max 1) and **2 to 4 instances in `postgres` mode** (`Mappings: Capacity` in `infra/app.yaml`, CPU target tracking at 50 percent); **never above 4**. The numpy vector index exists only in SQLite mode; in PostgreSQL mode cosine search runs in the database (pgvector, phase 21) and the in-memory index stays empty.
- In PostgreSQL mode any instance must be able to answer any request, so state that was process memory lives in the database: upload tickets (`upload_tickets`, SHA-256 of the ticket, used up by one `UPDATE ... RETURNING`) and the keyword-fallback toggle (`settings` row `fallback`, read at most every 5 seconds). Do not add new per-process state that a second instance would not see. The search rate limit stays per instance (the effective limit is `KB_SEARCH_RATE` times the instance count; `docs/SECURITY_REVIEW.md`, limitation 23). SQLite mode keeps tickets and the toggle in process memory.
- Two storage backends behind `kb/store/`: SQLite (default) and PostgreSQL with pgvector (when `KB_DATABASE_URL` is set, or read from S3 with `KB_DATABASE_URL_SOURCE=s3`). Code outside `kb/store/` and the schema modules (`kb/db.py`, `kb/store/schema_pg.sql`) writes SQL once with `?` placeholders and gets the dialect from the store; dialect-specific SQL (schema, full-text search, vectors) stays in the dialect modules. In PostgreSQL mode there is no snapshot thread and no commit hook; the S3 snapshot comes from `export-sqlite` (nightly cron and `POST /api/admin/export`).
- SQLite (the journal rule applies to SQLite mode only): `journal_mode=DELETE` (never WAL: the database must stay one self-contained file, because the snapshot copies it and a restore swaps it in whole), `foreign_keys=ON`, `busy_timeout=5000`. The database lives on the instance root volume (`/data/kb.db`) and is snapshotted to S3 after every write (`kb/snapshot.py`; the hook is in `kb/db.py`).
- Embedding and reranking run in a thread pool (`run_in_threadpool`), never on the event loop.
- Embeddings are stored as float32, L2-normalized BLOBs in `chunks.embedding` (PostgreSQL: `vector(384)`, handed to and from the code as the same bytes), with `chunks.embed_model` set.
- Every interface (REST, MCP over HTTP, MCP over stdio, UI, extension) calls `kb/service.py`. No business logic in `api.py`, `mcp_tools.py`, `ui.py` or templates.
- Logs are one JSON object per line. Stdout in server mode; **stderr in stdio MCP mode** (stdout carries the protocol).
- `GET /livez` (phase 25.3) is the ALB health check: 200 whenever the process answers HTTP; it reads no database, cache, model or worker thread, so a database outage or RDS failover never makes the load balancer replace every instance. Never add a dependency check to it.
- `GET /healthz` is readiness and diagnostics (the boot waits for it before `cfn-signal`; `scripts/demo_check.sh` reads it). It returns 200 whenever the database is readable, including in keyword mode. Model failure must never make the ALB kill the instance. In PostgreSQL mode it answers from a cache that `kb/health.py` (a daemon thread with its own connection) refreshes every 10 s (503 when the last success is over 60 s old); it never takes a pooled connection or a worker thread, and the same thread reads the fallback toggle, so no request path touches the pool for either.
- MCP over HTTP: the read tools (`search`, `get`, `list_tags`, `stale`, `gaps`) need no token; the write tools (`upload`, `upload_file`, `update`, `delete`, `merge`, `ingest_url`, `verify`) need `Authorization: Bearer <write token>` and fail closed (phase 24, PROGRESS.md deviation "MCP tools need no token", follow-up). MCP over stdio is local and trusted, so writes stay tokenless there. REST, UI and extension writes need a token too.
- Secrets never go in code, user data, Docker images or git. Write tokens come from `s3://<bucket>/config/write-tokens` on the instances (`KB_TOKENS_SOURCE=s3`, deviation 8), SSM `/kb/write-tokens` by default, or `KB_WRITE_TOKENS` locally.

## Configuration (`kb/config.py`, all from environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `KB_DB` | `/data/kb.db` | SQLite path |
| `KB_MODELS` | `on` | `off` skips model loading (keyword mode only, used by fast tests) |
| `KB_RERANK` | `on` | `off` skips the cross-encoder: candidates are ranked by cosine and judged by the `KB_COSINE_CONF_*` cutoffs (cut line 5) |
| `KB_MODEL_DIR` | `/models` | fastembed cache dir (baked into the image) |
| `KB_DATABASE_URL` | unset | `postgresql://user:password@host:5432/db?sslmode=require`. Unset: SQLite at `KB_DB`. Set: PostgreSQL (pool of 1 to 5 connections), `KB_DB` is ignored and snapshots are off. Carries the password: never logged, never in `repr(Settings)` |
| `KB_DATABASE_URL_SOURCE` | `env` | `s3` reads the URL from `s3://$KB_BUCKET/$KB_DATABASE_URL_KEY` (once per process) when `KB_DATABASE_URL` is unset. The app stack sets `s3` in `DatabaseMode=postgres` |
| `KB_DATABASE_URL_KEY` | `config/database-url` | Object in `KB_BUCKET` with the URL (`KB_DATABASE_URL_SOURCE=s3`) |
| `KB_BUCKET` | unset | S3 bucket for originals, snapshots and backups; unset means skip S3 (and snapshots) |
| `KB_SNAPSHOTS` | `on` | `off` stops the database snapshots to `s3://$KB_BUCKET/snapshots/kb-latest.db.gz` even when `KB_BUCKET` is set. Snapshots are also off when `KB_BUCKET` is unset |
| `KB_SNAPSHOT_INTERVAL` | `5` | Seconds a burst of writes is collected before one snapshot upload (at most one upload per interval while the database is dirty). Must be greater than 0 |
| `KB_TOKENS_SOURCE` | `ssm` | Where the write tokens are read from when `KB_WRITE_TOKENS` is unset: `ssm` or `s3`. The app stack sets `s3` (deviation 8) |
| `KB_TOKENS_PARAM` | `/kb/write-tokens` | SSM SecureString with `name:secret,name:secret` (`KB_TOKENS_SOURCE=ssm`) |
| `KB_TOKENS_KEY` | `config/write-tokens` | Object in `KB_BUCKET` with `name:secret,name:secret` (`KB_TOKENS_SOURCE=s3`) |
| `KB_WRITE_TOKENS` | unset | Local override for the token list; when set, AWS is not called |
| `KB_TOKEN` | unset | Optional for the stdio MCP entry point: if it matches a write token, writes are audited under that token's name, else as `mcp` |
| `AWS_REGION` | `us-east-1` | |
| `KB_CONF_HIGH` / `KB_CONF_LOW` | `0.70` / `0.02` | Semantic confidence cutoffs (tuned in phase 11 on the real corpus: reranker scores sit near 1 for answerable questions and near 0 for out-of-corpus ones) |
| `KB_COSINE_CONF_HIGH` / `KB_COSINE_CONF_LOW` | `0.75` / `0.55` | Confidence cutoffs used only when `KB_RERANK=off` |
| `KB_DUP_THRESHOLD` | `0.96` | Near-duplicate cosine cutoff (tuned in phase 11: planted copy 0.9989, highest distinct pair 0.9300) |
| `KB_SEARCH_TIMEOUT` | `5` | Seconds before a semantic search falls back to keyword (was 3; phase 11 measured over 3 s per search on the t3.medium) |
| `KB_SEMANTIC_CONCURRENCY` | `2` | Semantic searches that may run at once per instance (admission control; a t3.medium has 2 vCPU). A search that cannot get a slot within `KB_SEARCH_TIMEOUT` is answered by keyword search (log line "semantic search busy; using keyword search"). The slot is freed only when the worker threads of the search have finished, even if the search timed out. Must be at least 1 |
| `KB_RERANK_CANDIDATES` | `15` | Cosine matches handed to the cross-encoder (plus up to 5 FTS hits). The reranker's cost grows with this; without the reranker the cosine pool stays 30 |
| `KB_RERANK_MAX_CHARS` | `1000` | Each passage is cut to this many characters for reranking only (embedding and results use the whole chunk). Must be at least 100 |
| `KB_GAP_RESOLUTION` | `background` | When a write (upload, update, merge, ingest) resolves open gaps. `background`: the server queues the doc for one worker thread per process and answers at once (jobs still queued at shutdown are dropped; gap bookkeeping is best effort). `inline`: inside the request (the app-level tests use it). Stdio MCP and the CLI always resolve inline. Passages are cut to `KB_RERANK_MAX_CHARS` either way |
| `KB_PUBLIC_URL` | unset | Address clients reach the server at, used in upload links (`kb/uploads.py`). The app stack sets `https://<CloudFront domain>`; unset means the request's own base URL (fine locally) |
| `KB_EXTENSION_ORIGIN` | unset | `chrome-extension://<id>`, the only origin CORS allows (methods GET, POST, PATCH, DELETE; headers Authorization, Content-Type). Unset means no CORS headers at all. Set on the stack with the `ExtensionOrigin` parameter (`KB_EXTENSION_ORIGIN` for `infra/deploy.sh`) |
| `KB_SEARCH_RATE` | `120/minute` | Search rate limit per client address (raised from 30 in phase 24 so a class behind one campus NAT shares a workable budget; admission control, not this limit, protects against overload), one shared budget for `GET /api/search`, the UI search pages and the MCP `search` tool (stdio is unlimited). Over the limit: HTTP 429, or the tool error "Rate limit exceeded, try again in a minute". A malformed value stops startup |
| `KB_PROXY_HOPS` | `0` | Number of proxies in front of the app that each append to `X-Forwarded-For` (2 behind CloudFront and the ALB, set in the instance user data). The client address is the entry that many places from the right; `0` ignores the header. uvicorn's own proxy handling is off |
| `KB_METRICS_MODE` | `emf` | `emf`, `api` or `off`. `api` sends search metrics with `put_metric_data` instead of EMF log lines; `off` publishes nothing and starts no sender (a plain "search metrics" log line only). The stack parameter `MetricsMode` defaults to `off` because the sandbox instance role cannot publish metrics; the `KbMcp` and `CWAgent` alarms and the `kb-mcp-search` dashboard exist only when it is not `off` (phase 25.6) |
| `KB_TEST_DATABASE_URL` | unset | **Test only.** A scratch PostgreSQL database for the tests (their tables are emptied before each test). Unset skips the PostgreSQL legs |
| `KB_FAKE_MODELS` | unset | **Test only.** `1` swaps in the doubles from `tests/fakes.py` for the real models (inert unless `tests/` is importable, so inert in the image). Never set it in a deployment |

## AWS Academy sandbox rules

The environment is the AWS Academy Cloud Architecting 3 sandbox (probed 2026-10-08).

Allowed (per the lab list): CloudFormation, CloudFront, CloudWatch, EC2 (`t3.medium` allowed, EBS `gp2` only and up to 35 GB, at most 9 instances or the account is suspended), Auto Scaling, ELB, IAM (read only), KMS (list only), Lambda, RDS, Route 53 (no domain registration), S3, S3 Glacier, SNS, SSM (Session Manager is listed, not yet tested), CloudShell, CloudTrail, DynamoDB, Cloud9. EventBridge is not listed.

Blocked: **ECR** (`ecr:CreateRepository` is AccessDenied) and **EFS** (`elasticfilesystem:CreateFileSystem` is AccessDenied), both probed from the lab terminal role `runserver`. So the image travels as a tarball through S3 (`infra/push-image.sh`, user data `docker load`) and the database lives on the instance root volume with S3 snapshots instead of a shared file system.

- Region `us-east-1` only.
- No custom IAM roles or policies (IAM is read only). `LabInstanceProfile` does not exist here, and the lab user may only pass `myS3Role` to an instance (`iam:PassRole` denies `EMR_EC2_DefaultRole`). `myS3Role` allows S3 and nothing else (deviation 8 in `docs/PROGRESS.md`). Do not add anything the instance needs beyond S3; custom CloudWatch metrics cannot be published from it.
- KMS is list only, so an encrypted root volume may fail with `Client.InternalError`; the fallback is `Encrypted: false`, recorded in `docs/PROGRESS.md`.
- More than 9 instances suspends the account. The Auto Scaling group is max 1 in `sqlite` mode and max 4 in `postgres` mode; never launch other instances by hand.
- Credentials are temporary. If an AWS call fails with `ExpiredToken` or `InvalidClientTokenId`, stop and ask the user to refresh `~/.aws/credentials` from the AWS Academy console (**HUMAN**).
- Stacks: `kb-network`, `kb-data`, `kb-db`, `kb-app`. Never delete `kb-network` or `kb-data`. `kb-app` may be deleted between sessions to save credit; the next deploy restores the database from the S3 snapshot. `kb-db` (RDS PostgreSQL with pgvector, phase 19) may be deleted between sessions only after an export to S3 (phase 20), because deleting it destroys the data.
- Always run `cfn-lint` and `aws cloudformation validate-template` before deploying.
- Ask the user before any destructive AWS action (deleting a stack, terminating an instance), except where a phase file explicitly says to do it.
