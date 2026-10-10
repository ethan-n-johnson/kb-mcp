# kb-mcp

Knowledge-base MCP server for the Cloud Computing course (AWS Academy Cloud Architecting). One FastAPI service on AWS: semantic search with local CPU models, keyword fallback, 11 MCP tools, REST, web UI, Chrome capture extension. Demo: Wed Oct 14, 2026.

- Full design: `docs/PLAN.md` (source of truth for behavior and numbers)
- Work breakdown: `docs/phases/` (one file per phase, index in `docs/phases/README.md`)
- Progress log, stack outputs, deviations: `docs/PROGRESS.md`
- Computer and AWS credentials setup (Linux and Windows): `docs/SETUP.md`

## How to work in this repo

1. Do exactly one phase per session. Start by reading the phase file, then `docs/PROGRESS.md`, then only the `docs/PLAN.md` sections the phase lists.
2. Stay inside the phase's scope. If something outside it is broken or missing, add it to "Open issues" in `docs/PROGRESS.md` instead of fixing it, unless the phase cannot pass without the fix.
3. Steps marked **HUMAN** need the user (AWS Academy console, credentials, secrets, email inboxes, Chrome, Claude Code config). Stop, tell the user exactly what to do, and wait. Never work around them.
4. If the PLAN and reality disagree (a library API differs, AWS rejects a setting), pick the smallest change that keeps the PLAN's intent, and record it under "Deviations" in `docs/PROGRESS.md`.
5. A phase is done only when every acceptance check in its file passes. Then append a row to the phase log in `docs/PROGRESS.md` and make one commit: `phase NN: <title>`.
6. Never use em dashes in docs, code comments, commit messages or UI text.
7. Never print, log, commit or echo secret values (write tokens, AWS keys). Check that a secret exists; do not display it.

## Stack

Python 3.12, FastAPI, uvicorn, official `mcp` SDK 2.x (`MCPServer`, called `FastMCP` in 1.x), fastembed, numpy, SQLite with FTS5, trafilatura (>=2.0), pymupdf4llm, httpx, Jinja2 + htmx, nh3, slowapi, boto3, PyYAML. Dev: pytest, moto, cfn-lint.

Virtualenv at `.venv`; install with `pip install -e '.[dev]'`.

## Commands

- Fast tests (models off): `pytest -q`
- Model tests (loads real ONNX models, slower): `pytest -q -m models`
- Run locally: `KB_DB=./.localdata/kb.db KB_WRITE_TOKENS=dev:devtoken uvicorn kb.main:app --port 8000 --workers 1`
- Stdio MCP: `KB_DB=./.localdata/kb.db KB_WRITE_TOKENS=dev:devtoken KB_TOKEN=devtoken python -m kb.cli mcp-stdio` (`KB_TOKEN` is checked against the token list, so the list must be available)
- Seed: `KB_DB=./.localdata/seed/kb.db KB_MODEL_DIR=./.localdata/models python -m kb.cli seed seeds/seeds.yaml` (`--dry-run` checks every entry without writing); then `python -m kb.cli eval tests/eval/questions.yaml` and `KB_BUCKET=<bucket> python -m kb.cli snapshot-push` (docs/SETUP.md, "Seed the demo corpus")
- Lint infra: `cfn-lint infra/*.yaml`
- Deploy a stack: `infra/deploy.sh network|data|app [ImageTag]` (created in phases 01 and 06)

## Runtime rules (never break these)

- uvicorn runs with exactly **1 worker**; the Auto Scaling group stays at **max 1**. The vector index lives in process memory.
- SQLite: `journal_mode=DELETE` (never WAL: the database must stay one self-contained file, because the snapshot copies it and a restore swaps it in whole), `foreign_keys=ON`, `busy_timeout=5000`. The database lives on the instance root volume (`/data/kb.db`) and is snapshotted to S3 after every write (`kb/snapshot.py`; the hook is in `kb/db.py`).
- Embedding and reranking run in a thread pool (`run_in_threadpool`), never on the event loop.
- Embeddings are stored as float32, L2-normalized BLOBs in `chunks.embedding`, with `chunks.embed_model` set.
- Every interface (REST, MCP over HTTP, MCP over stdio, UI, extension) calls `kb/service.py`. No business logic in `api.py`, `mcp_tools.py`, `ui.py` or templates.
- Logs are one JSON object per line. Stdout in server mode; **stderr in stdio MCP mode** (stdout carries the protocol).
- `GET /healthz` returns 200 whenever the database is readable, including in keyword mode. Model failure must never make the ALB kill the instance.
- Secrets never go in code, user data, Docker images or git. Write tokens come from `s3://<bucket>/config/write-tokens` on the instances (`KB_TOKENS_SOURCE=s3`, deviation 8), SSM `/kb/write-tokens` by default, or `KB_WRITE_TOKENS` locally.

## Configuration (`kb/config.py`, all from environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `KB_DB` | `/data/kb.db` | SQLite path |
| `KB_MODELS` | `on` | `off` skips model loading (keyword mode only, used by fast tests) |
| `KB_RERANK` | `on` | `off` skips the cross-encoder: candidates are ranked by cosine and judged by the `KB_COSINE_CONF_*` cutoffs (cut line 5) |
| `KB_MODEL_DIR` | `/models` | fastembed cache dir (baked into the image) |
| `KB_BUCKET` | unset | S3 bucket for originals, snapshots and backups; unset means skip S3 (and snapshots) |
| `KB_SNAPSHOTS` | `on` | `off` stops the database snapshots to `s3://$KB_BUCKET/snapshots/kb-latest.db.gz` even when `KB_BUCKET` is set. Snapshots are also off when `KB_BUCKET` is unset |
| `KB_SNAPSHOT_INTERVAL` | `5` | Seconds a burst of writes is collected before one snapshot upload (at most one upload per interval while the database is dirty). Must be greater than 0 |
| `KB_TOKENS_SOURCE` | `ssm` | Where the write tokens are read from when `KB_WRITE_TOKENS` is unset: `ssm` or `s3`. The app stack sets `s3` (deviation 8) |
| `KB_TOKENS_PARAM` | `/kb/write-tokens` | SSM SecureString with `name:secret,name:secret` (`KB_TOKENS_SOURCE=ssm`) |
| `KB_TOKENS_KEY` | `config/write-tokens` | Object in `KB_BUCKET` with `name:secret,name:secret` (`KB_TOKENS_SOURCE=s3`) |
| `KB_WRITE_TOKENS` | unset | Local override for the token list; when set, AWS is not called |
| `KB_TOKEN` | unset | Token used by the stdio MCP entry point; must be one of the write tokens |
| `AWS_REGION` | `us-east-1` | |
| `KB_CONF_HIGH` / `KB_CONF_LOW` | `0.70` / `0.02` | Semantic confidence cutoffs (tuned in phase 11 on the real corpus: reranker scores sit near 1 for answerable questions and near 0 for out-of-corpus ones) |
| `KB_COSINE_CONF_HIGH` / `KB_COSINE_CONF_LOW` | `0.75` / `0.55` | Confidence cutoffs used only when `KB_RERANK=off` |
| `KB_DUP_THRESHOLD` | `0.96` | Near-duplicate cosine cutoff (tuned in phase 11: planted copy 0.9989, highest distinct pair 0.9300) |
| `KB_SEARCH_TIMEOUT` | `3` | Seconds before a semantic search falls back to keyword |
| `KB_EXTENSION_ORIGIN` | unset | `chrome-extension://<id>`, the only origin CORS allows (methods GET, POST, PATCH, DELETE; headers Authorization, Content-Type). Unset means no CORS headers at all. Set on the stack with the `ExtensionOrigin` parameter (`KB_EXTENSION_ORIGIN` for `infra/deploy.sh`) |
| `KB_SEARCH_RATE` | `30/minute` | Search rate limit per client address, one shared budget for `GET /api/search`, the UI search pages and the MCP `search` tool (stdio is unlimited). Over the limit: HTTP 429, or the tool error "Rate limit exceeded, try again in a minute". A malformed value stops startup |
| `KB_PROXY_HOPS` | `0` | Number of proxies in front of the app that each append to `X-Forwarded-For` (2 behind CloudFront and the ALB, set in the instance user data). The client address is the entry that many places from the right; `0` ignores the header. uvicorn's own proxy handling is off |
| `KB_METRICS_MODE` | `emf` | `api` sends search metrics with `put_metric_data` instead of EMF log lines |
| `KB_FAKE_MODELS` | unset | **Test only.** `1` swaps in the doubles from `tests/fakes.py` for the real models (inert unless `tests/` is importable, so inert in the image). Never set it in a deployment |

## AWS Academy sandbox rules

The environment is the AWS Academy Cloud Architecting 3 sandbox (probed 2026-10-08).

Allowed (per the lab list): CloudFormation, CloudFront, CloudWatch, EC2 (`t3.medium` allowed, EBS `gp2` only and up to 35 GB, at most 9 instances or the account is suspended), Auto Scaling, ELB, IAM (read only), KMS (list only), Lambda, RDS, Route 53 (no domain registration), S3, S3 Glacier, SNS, SSM (Session Manager is listed, not yet tested), CloudShell, CloudTrail, DynamoDB, Cloud9. EventBridge is not listed.

Blocked: **ECR** (`ecr:CreateRepository` is AccessDenied) and **EFS** (`elasticfilesystem:CreateFileSystem` is AccessDenied), both probed from the lab terminal role `runserver`. So the image travels as a tarball through S3 (`infra/push-image.sh`, user data `docker load`) and the database lives on the instance root volume with S3 snapshots instead of a shared file system.

- Region `us-east-1` only.
- No custom IAM roles or policies (IAM is read only). `LabInstanceProfile` does not exist here, and the lab user may only pass `myS3Role` to an instance (`iam:PassRole` denies `EMR_EC2_DefaultRole`). `myS3Role` allows S3 and nothing else (deviation 8 in `docs/PROGRESS.md`). Do not add anything the instance needs beyond S3; custom CloudWatch metrics cannot be published from it.
- KMS is list only, so an encrypted root volume may fail with `Client.InternalError`; the fallback is `Encrypted: false`, recorded in `docs/PROGRESS.md`.
- More than 9 instances suspends the account. The Auto Scaling group is max 1; never launch other instances.
- Credentials are temporary. If an AWS call fails with `ExpiredToken` or `InvalidClientTokenId`, stop and ask the user to refresh `~/.aws/credentials` from the AWS Academy console (**HUMAN**).
- Stacks: `kb-network`, `kb-data`, `kb-app`. Never delete `kb-network` or `kb-data`. `kb-app` may be deleted between sessions to save credit; the next deploy restores the database from the S3 snapshot.
- Always run `cfn-lint` and `aws cloudformation validate-template` before deploying.
- Ask the user before any destructive AWS action (deleting a stack, terminating an instance), except where a phase file explicitly says to do it.
