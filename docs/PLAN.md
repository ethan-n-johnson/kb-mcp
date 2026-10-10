# Knowledge-Base MCP Server: Build Plan

Oct 7, 2026 · @temp email

## Summary

Build one FastAPI service on AWS using the reference pattern from AWS Academy Cloud Architecting: a custom VPC across two Availability Zones, an Application Load Balancer, an Auto Scaling group, shared file storage, and CloudFormation for every resource. All models run locally on CPU, so no model API access is needed. Target demo date: Wednesday, Oct 14, 2026.

| Decision | Choice | Why |
| --- | --- | --- |
| Embeddings | `BAAI/bge-small-en-v1.5` via fastembed (384 dims, ONNX, CPU) | No API access; no PyTorch in the image; good quality for its size |
| Reranker | `Xenova/ms-marco-MiniLM-L-6-v2` cross-encoder via fastembed `TextCrossEncoder` | Small enough to rerank about 30 candidates per query on CPU |
| Fallback | SQLite FTS5 (BM25) | Used when the local model fails to load, errors or times out, plus a demo toggle |
| Network | Custom VPC in us-east-1, public and private subnets in 2 AZs, internet gateway, one NAT gateway, S3 gateway endpoint | App servers never get a public IP |
| Compute | EC2 `t3.medium` from a launch template, in an Auto Scaling group (min 1, max 1) spanning both private subnets | Self-healing across AZs; one instance because the vector index lives in process memory |
| Storage | SQLite on Amazon EFS; original files in S3 (versioned); nightly database backups to S3 | EFS is reachable from both AZs, so a replacement instance finds the data; EBS is tied to one AZ (superseded: see Sandbox changes) |
| Edge | ALB in the public subnets; CloudFront in front for HTTPS on its default `*.cloudfront.net` certificate | TLS without buying a domain, since ACM certificates need one |
| Access | Public reads; bearer token on every write; tokens stored in SSM Parameter Store | Easy to demo live while keeping the corpus safe |
| Operations | 3 CloudFormation stacks; CloudWatch Logs, metrics, alarms; SNS email alerts | Repeatable deploys and teardown inside sandbox sessions |
| Scale | One instance, up to about 10k chunks in memory (about 15 MB of vectors) | Scale-out path documented in Course alignment, not built |
| In scope | Seeded demo corpus; Chrome capture extension | Chosen in the clarifying questions |
| Out of scope | Multi-instance writes, link crawler (`ingest_url` fetches one page), user accounts or SSO, OCR for scanned PDFs | Not needed to show the core idea in a week |

## Sandbox changes (2026-10-08)

Probes from the lab terminal role `runserver` on 2026-10-08 showed that `ecr:CreateRepository` and `elasticfilesystem:CreateFileSystem` are both AccessDenied in the AWS Academy Cloud Architecting 3 sandbox. The sandbox allows CloudFormation, CloudFront, CloudWatch, EC2 (`t3.medium`, EBS `gp2` only up to 35 GB, at most 9 instances or the account is suspended), Auto Scaling, ELB, IAM (read only), KMS (list only), Lambda, RDS, Route 53, S3, S3 Glacier, SNS, SSM, CloudShell, CloudTrail, DynamoDB and Cloud9. The original plan text below is kept as written; wherever it says EFS or ECR, this section replaces it.

**1. Image delivery: an S3 tarball instead of ECR.** `infra/push-image.sh` runs `docker save | gzip` straight into `s3://<bucket>/images/kb-mcp-<tag>.tar.gz` and checks the object afterwards. The instance user data downloads it (through the S3 gateway endpoint, not the NAT gateway), runs `gunzip -c | docker load` and starts `kb-mcp:<tag>`. A lifecycle rule expires `images/` after 30 days.

**2. Database: local disk plus S3 snapshots instead of EFS.**

- SQLite lives on the instance root volume at `/data/kb.db` (a plain directory owned by uid 10001, bind-mounted into the container). The rollback journal stays (`journal_mode=DELETE`): the snapshot copies one self-contained file.
- **Snapshot on write.** Every committed write marks the database dirty (a hook on the SQLite connection, so uploads, updates, deletes, merges, verify, gap rows from low-confidence searches, fallback toggle audit rows and the startup backfill are all covered). One background thread uploads a consistent snapshot (SQLite backup API, integrity check, gzip) to `s3://<bucket>/snapshots/kb-latest.db.gz` at most once every `KB_SNAPSHOT_INTERVAL` seconds (default 5) while the database is dirty. Bucket versioning keeps the history. A failed upload is logged and retried one interval later, never raised into a request.
- **Flush on shutdown.** The FastAPI lifespan exit (SIGTERM from `docker stop`) uploads a last snapshot if one is pending; the container runs with `--stop-timeout 30`.
- **Restore at boot.** Before the service container starts, user data runs a one-off container: `python -m kb.cli restore --latest --force --target /data/kb.db`. Source order: `snapshots/kb-latest.db.gz`, else the newest object under `backups/`, else start empty. If S3 cannot be read the boot fails and is retried; it never starts empty over a real snapshot.
- **Nightly backups** under `backups/` stay as they were (longer history for the disaster planning requirement).
- The Auto Scaling group still spans both private subnets, so a replacement can land in the other AZ.

**3. Instance role (2026-10-09).** `LabInstanceProfile` does not exist in this sandbox, IAM is read only, and the lab user may only pass the existing `myS3Role` profile to an instance, which allows S3 and nothing else. So the write tokens live in the private data bucket at `config/write-tokens` and the app reads them through the instance role (instead of the SSM SecureString below), container logs stay on the instance instead of going to a CloudWatch log group, and the custom search and memory metrics cannot be published (the ALB metrics and alarms still work). Details: deviation 8 in `docs/PROGRESS.md`.

**Trade-offs.** The recovery point is a few seconds (the snapshot interval plus the upload time) instead of zero with EFS; a hard kill with no SIGTERM loses the writes of the last interval, a graceful stop loses none. Multi-AZ is kept: S3 is reachable from both AZs, so a replacement restores wherever it starts. S3 is the durable store, the instance disk is a working copy. Boot takes longer (image download and restore before the app starts). A bad delete is copied into the snapshot within seconds, so recovering from one still uses a nightly backup (`--key backups/...`) or an earlier S3 version of the snapshot. Writes still go through one instance, so the single-writer rule is unchanged.

Updated rows:

| Line | Replacement |
| --- | --- |
| Storage (Summary table) | SQLite on the instance root volume (`gp2`, encrypted if KMS allows), snapshotted to S3 within seconds of every write; original files in S3 (versioned); nightly database backups to S3. Why: S3 is reachable from both AZs, so a replacement instance in either AZ restores the latest snapshot at boot; EFS is blocked in the sandbox |
| Disaster planning (Course alignment) | Continuous snapshots plus nightly SQLite backups in S3; restore script; instance or AZ failure recovers in a few minutes from the latest snapshot with a recovery point of a few seconds (a graceful stop loses nothing); corruption or a bad delete restores from the last nightly backup (RPO up to 24 hours) or from an earlier version of the snapshot |

## Course alignment

The project reuses the course's layered build (network, compute, storage, edge, monitoring, automation) and argues every trade-off against the Well-Architected Framework, which is what SAA-C03 tests.

| Course topic | Where it shows up in this project |
| --- | --- |
| Securing access (IAM) | Sandbox `LabRole` and `LabInstanceProfile` instead of custom roles; no access keys on the instance; IMDSv2 required |
| Storage layer (S3) | Bucket for original uploads and backups; versioning, SSE-S3 encryption, Block Public Access on |
| Compute layer (EC2) | Launch template with user data, `t3.medium` sizing, image pulled from ECR (superseded: see Sandbox changes) |
| Database layer | SQLite chosen over RDS, with the trade-off written up (cost, single writer); RDS PostgreSQL with pgvector as the scale-out path |
| Networking (VPC) | Custom VPC, public and private subnets in 2 AZs, route tables, internet gateway, NAT gateway |
| Connecting networks | S3 gateway endpoint so backups never cross the NAT gateway |
| Securing user and application access | Security group chaining: CloudFront prefix list to ALB, ALB to app, app to EFS (superseded: see Sandbox changes); bearer tokens in Parameter Store |
| Monitoring, elasticity, high availability | ALB health checks on `/healthz`; Auto Scaling group replaces a failed instance in either AZ; CloudWatch alarms to SNS |
| Automating your architecture | All resources in CloudFormation, split into network, data and app stacks |
| Caching content | CloudFront for HTTPS and caching of static UI assets; API and MCP paths pass through uncached |
| Decoupled and serverless architectures | Stretch: weekly EventBridge-scheduled Lambda emails the `stale` and `gaps` reports through SNS |
| Disaster planning | Nightly SQLite backup to S3; restore script; instance or AZ failure loses no data (it lives on EFS) and recovers in a few minutes; corruption or a bad delete restores from the last backup (RPO up to 24 hours) (superseded: see Sandbox changes) |

| Well-Architected pillar | Decisions |
| --- | --- |
| Operational excellence | Infrastructure as code; deploys by pushing a new image and starting an ASG instance refresh; structured logs in CloudWatch |
| Security | Private subnets; least-open security groups; HTTPS at the edge; encryption at rest on EFS and S3 (superseded: see Sandbox changes); tokens never in code or user data |
| Reliability | Multi-AZ subnets and EFS (superseded: see Sandbox changes); self-healing ASG; versioned S3 backups; keyword fallback keeps search working if models fail |
| Performance efficiency | Vectors in memory, so search needs no database round trip; ONNX models on CPU; instance size checked against CloudWatch CPU and memory |
| Cost optimization | No vector database or RDS; one NAT gateway instead of one per AZ; burstable instance; app stack deleted between sessions |
| Sustainability | Small CPU models; one right-sized instance; idle resources torn down |

The deliberate weak spot: one instance means a few minutes of downtime while the Auto Scaling group replaces it. That is the right call for a prototype, and the scale-out path (RDS with pgvector, stateless app instances, ASG max above 1) is the natural discussion point in the presentation.

## Architecture

Every interface (MCP over HTTP, MCP over stdio, REST, web UI, extension) calls one shared service layer, so a tool and its REST route can never drift apart. The process holds the vector matrix in memory and rebuilds it from SQLite on startup.

(Diagram in the original plan: application architecture, one service layer. README.md gets a Mermaid version in phase 17.)

The stdio entry point runs the same tools on a laptop against a local copy of the database, skipping AWS and the network entirely.

Runtime rules that follow from the in-memory index:

- Run uvicorn with **one worker** and keep the Auto Scaling group at **max 1**. A second process would hold its own copy of the index and diverge after a write.
- Run embedding and reranking in a thread pool (`run_in_threadpool`) so a slow query does not block the event loop.
- Store each chunk's embedding as a float32 BLOB in SQLite, so a replacement instance rebuilds the index from EFS without re-embedding.
- Use SQLite's rollback journal (`journal_mode=DELETE`), not WAL, on EFS. WAL needs shared memory that network file systems do not provide reliably.
- Write logs as JSON to stdout; the CloudWatch agent ships them to a log group.

Key libraries: FastAPI, the official `mcp` Python SDK (`FastMCP`), fastembed, numpy, trafilatura, pymupdf4llm, httpx, Jinja2 + htmx, nh3 (HTML sanitizing), slowapi (rate limits).

The MCP server is mounted inside FastAPI at `/mcp` using `mcp.streamable_http_app()`, with the MCP session manager started in FastAPI's lifespan. Configure FastMCP with `stateless_http=True` and `json_response=True`: each request is self-contained, so the ALB and CloudFront never have to hold a long-lived stream or pin a session to one instance. The stdio entry point (`python -m kb.cli mcp-stdio`) builds the same tool set and calls `mcp.run(transport="stdio")` against a local database.

```text
kb-mcp/
  kb/
    main.py            FastAPI app: mounts /mcp, /api, UI; lifespan loads models + index
    config.py          settings from env and SSM Parameter Store
    db.py              schema, FTS5 triggers, connection helpers
    service.py         shared operations used by MCP, REST and UI
    ingest/
      fetch.py         httpx fetch with SSRF guard and size limits
      convert.py       HTML/URL to MD (trafilatura), PDF to MD (pymupdf4llm), MD passthrough
      chunk.py         heading-boundary splitter
      dedupe.py        content hash + embedding similarity checks
      originals.py     saves uploaded files to S3
    search/
      embedder.py      fastembed wrapper with health flag and timeout
      index.py         in-memory numpy matrix: load, add, remove, top-k
      reranker.py      cross-encoder scoring
      pipeline.py      semantic path, keyword fallback, confidence, gap logging
    metrics.py         CloudWatch embedded-metric-format log lines
    mcp_tools.py       the 11 MCP tools
    api.py             REST routes
    auth.py            bearer-token dependency, constant-time compare
    cli.py             stdio entry point, seed, backup, restore
    templates/         Jinja2 pages for the UI
  extension/           Chrome MV3 capture extension
  seeds/seeds.yaml     demo corpus manifest
  infra/
    network.yaml       VPC, subnets, route tables, IGW, S3 gateway endpoint
    data.yaml          EFS, S3 bucket, ECR repo, Parameter Store entries (retained on delete)
    app.yaml           NAT gateway, ALB, launch template, ASG, CloudFront, alarms, SNS
    user-data.sh       installs Docker and CloudWatch agent, mounts EFS, pulls image
  Dockerfile
  tests/
```

## Ingestion and data model

Every input becomes clean Markdown first, then one chunk per heading section, so search results can cite a heading path like "EBS > Attach a volume > Linux".

1. **Detect the format.** URL, raw HTML, PDF or Markdown, from the request field, file extension or response `Content-Type`.
2. **Convert to Markdown.**
   - HTML and URLs: `trafilatura.extract(..., output_format="markdown", include_tables=True, include_links=True)`. Trafilatura keeps the main content and drops nav bars, footers, cookie banners and sidebars.
   - PDF: `pymupdf4llm.to_markdown()`, which keeps headings and tables. A PDF with no text layer is rejected with a clear error (no OCR).
   - Markdown: passed through; YAML front matter fills `title` and `tags`.
3. **Clean up.** Collapse blank lines, drop empty headings and leftover "Skip to content" or "Edit this page" lines with a small regex list.
4. **Chunk on headings.** Split at H1 to H3. Each chunk keeps its heading path. Merge sections under about 50 words into the next one. Split sections over about 350 words at paragraph breaks, since bge-small reads at most 512 tokens.
5. **Deduplicate** (upload and `ingest_url`): see the duplicate rules below.
6. **Embed and store.** Embed `title > heading path` plus the chunk text, write rows in one transaction, then add the vectors to the in-memory index. Uploaded PDF and HTML files are also saved to S3 under originals/\<doc\_id>/, so documents can be reprocessed later with a better converter.

Duplicate rules:

- **Exact:** SHA-256 of the normalized Markdown matches an existing doc, so the upload is refused and the existing `doc_id` is returned.
- **Near:** the new doc's vector (the mean of its chunk vectors) has cosine 0.92 or higher with an existing doc, so the call returns `possible_duplicate` with candidates. The caller then chooses `update`, `merge`, or re-sends with `force=true`. The 0.92 starting value gets tuned on the demo corpus.
- **Model down:** only the exact check runs, and the response says so.

SQLite schema (file at /data/kb.db on EFS, rollback journal mode):

| Table | Key columns | Purpose |
| --- | --- | --- |
| `docs` | `id`, `title`, `source_url`, `source_type`, `content_md`, `content_hash`, `created_at`, `updated_at`, `verified_at`, `verified_by` | One row per document; `content_md` is the source of truth for re-chunking; s3\_key points to the original file |
| `chunks` | `id`, `doc_id`, `ord`, `heading_path`, `text`, `embedding` (BLOB), `embed_model` | Search unit; `embed_model` lets you detect vectors from an older model |
| `chunks_fts` | FTS5 over `heading_path`, `text` (external content = `chunks`, porter tokenizer) | Keyword fallback, kept in sync by insert, update and delete triggers |
| `doc_tags` | `doc_id`, `tag` | Powers `list_tags` and tag filters |
| `gaps` | `id`, `query`, `query_norm`, `top_score`, `mode`, `created_at`, `resolved_doc_id` | Questions with no good answer |
| `audit_log` | `id`, `token_name`, `action`, `doc_id`, `at` | Who changed what, shown in the UI |

## Search

Search runs semantic retrieval plus rerank when the local models are healthy, and FTS5 keyword search otherwise; every response says which mode answered and how confident it is.

Semantic path:

1. Embed the query with the bge query prefix ("Represent this sentence for searching relevant passages: ").
2. Score every chunk with one matrix product. Vectors are L2-normalized at insert, so the dot product equals cosine similarity.
3. Take the top 30 with `numpy.argpartition`, then union in the FTS5 top 10 so exact identifiers (error codes, flag names) are not missed.
4. Rerank the union with the cross-encoder, apply the sigmoid to get a 0 to 1 score, keep at most 2 chunks per doc, return the top `k` (default 5).

Keyword fallback runs FTS5 `MATCH` with BM25 ranking and `snippet()` highlights. It triggers when:

- the embedder or reranker failed to load at startup,
- inference raises an error or takes longer than 3 seconds,
- or the admin toggle `POST /api/admin/fallback` (token required) is on, for the demo.

`GET /healthz` reports the current mode, model status and chunk count; the ALB target group uses it as the health check. Each search also logs CloudWatch metrics (latency, mode, confidence), and an alarm on keyword mode emails you through SNS.

Confidence flag (starting thresholds, tuned on Day 5 against about 20 hand-written test questions):

| Mode | high | medium | low |
| --- | --- | --- | --- |
| Semantic (rerank score) | 0.70 or above | 0.30 to 0.70 | below 0.30, or no results |
| Keyword (BM25) | never | top hit matches all query terms | partial term match, or no results |

Keyword mode is capped at `medium` because BM25 scores are not comparable across queries.

Gap logging: every `low` result writes a row to `gaps` with the query, a normalized form (lowercase, stop words removed) and the top score. The `gaps` tool groups by the normalized form and sorts by count, so the most-asked unanswered questions surface first. A later `upload` or `ingest_url` that answers a gap can mark it resolved.

Response shape shared by MCP and REST:

```json
{
  "mode": "semantic",
  "confidence": "high",
  "results": [
    {
      "doc_id": 12, "chunk_id": 340, "title": "Amazon EBS volumes",
      "heading_path": "Attach a volume > Linux", "snippet": "...",
      "score": 0.83, "source_url": "https://...", "verified_at": "2026-10-10"
    }
  ]
}
```

## Interfaces

The 11 MCP tools and the REST routes map one to one onto `service.py`; write tools check the token before touching the database.

| MCP tool | Arguments | Token | REST route | What it does |
| --- | --- | --- | --- | --- |
| `search` | `query`, `k=5`, `tags?` | No | `GET /api/search` | Pipeline above; returns mode, confidence, results |
| `get` | `doc_id` or `chunk_id`, `include_chunks=false` | No | `GET /api/docs/{id}` | Full Markdown plus metadata and tags |
| `upload` | `title`, `content`, `format`, `tags?`, `force=false` | Yes | `POST /api/docs` | Convert, dedupe, chunk, embed, store |
| `update` | `doc_id`, `content?`, `title?`, `tags?`, `format?` | Yes | `PATCH /api/docs/{id}` | Re-chunks and re-embeds only if content changed |
| `delete` | `doc_id` | Yes | `DELETE /api/docs/{id}` | Removes rows, FTS entries and index vectors |
| `merge` | `target_id`, `source_ids`, `delete_sources=true` | Yes | `POST /api/docs/merge` | Appends sources as sections of the target, keeps their URLs, re-chunks |
| `ingest_url` | `url`, `tags?`, `force=false` | Yes | `POST /api/ingest` | Fetches one page, then the `upload` path |
| `list_tags` | none | No | `GET /api/tags` | Tags with doc counts |
| `verify` | `doc_id`, `note?` | Yes | `POST /api/docs/{id}/verify` | Sets `verified_at` and `verified_by` |
| `stale` | `days=90`, `limit=20` | No | `GET /api/stale` | Docs not verified or updated in N days, oldest first |
| `gaps` | `limit=20`, `include_resolved=false` | No | `GET /api/gaps` | Unanswered questions, most frequent first |

Tool descriptions matter more than usual here, because Claude Code picks tools from them. Each description says when to use the tool, e.g. "Search internal docs before answering questions about our systems; if confidence is low, say so instead of guessing."

Connecting Claude Code:

```bash
# Remote, read and write
claude mcp add --transport http kb https://<distribution>.cloudfront.net/mcp \
  --header "Authorization: Bearer $KB_TOKEN"

# Local stdio against a copy of the database
claude mcp add kb-local -e KB_DB=./kb.db -e KB_TOKEN=$KB_TOKEN \
  -- python -m kb.cli mcp-stdio
```

Web UI (server-rendered Jinja2 with htmx, no build step):

- **Search page:** query box, results with heading path, snippet, score, and badges for confidence and mode.
- **Doc page:** rendered Markdown (sanitized with nh3), tags, source link, verified date, Verify button.
- **Add page:** paste Markdown, upload a file, or enter a URL; shows duplicate warnings with Update, Merge or Save anyway.
- **Maintenance page:** stale docs, top gaps, recent audit log, fallback toggle.
- The write token is entered once and kept in `sessionStorage`; htmx sends it as a header on write requests.

## Auth and security

Reads are public and every write needs a named bearer token; because the service is on the open internet, the main risks are SSRF through `ingest_url`, CPU abuse through search, and stored XSS through uploaded HTML.

- **Tokens:** one SecureString in SSM Parameter Store, `/kb/write-tokens` = `temp:<random>,demo:<random>`, read at startup through the instance profile. Generate each with `secrets.token_urlsafe(32)`, compare with `hmac.compare_digest`, and record the token name in `audit_log`. Revoke the demo token after the presentation by editing the parameter.
- **Where the token travels:** `Authorization: Bearer` on REST and MCP over HTTP; the `KB_TOKEN` env var for stdio; extension settings for captures.
- **HTTPS:** CloudFront redirects HTTP to HTTPS for viewers. Its origin request policy forwards the `Authorization` header, and `/api/*` and `/mcp*` use the CachingDisabled policy so no response is ever shared between users.
- **Security group chain:** ALB accepts port 80 only from the CloudFront origin-facing managed prefix list; app instances accept 8000 only from the ALB group; EFS accepts 2049 only from the app group.
- **No SSH:** instances have no public IP and no port 22. Use Session Manager for a shell; if the sandbox blocks it, add a bastion host in a public subnet, as in the course labs.
- **SSRF guard:** `ingest_url` resolves the hostname and refuses private, loopback and link-local addresses, including `169.254.169.254` (instance metadata). It re-checks after each redirect, allows only http and https, and caps downloads at 20 MB.
- **Instance metadata:** the launch template requires IMDSv2 with a hop limit of 1, so code inside the container cannot read instance credentials.
- **Encryption at rest:** EFS encrypted; S3 with SSE-S3 and Block Public Access on.
- **Abuse limits:** slowapi at 30 searches per minute per client IP (taken from `X-Forwarded-For`); 20 MB upload limit; 300-page PDF limit.
- **XSS and CORS:** sanitize rendered Markdown with nh3; allow only the extension's `chrome-extension://<id>` origin on write routes.
- **Known limitation:** the CloudFront to ALB hop is plain HTTP, and the MCP spec defines OAuth 2.1 for remote servers. A custom domain with an ACM certificate on the ALB, plus OAuth, is the production upgrade worth naming in your presentation.

## AWS deployment

Deploy three CloudFormation stacks in us-east-1, one of the regions AWS Academy sandboxes allow. Network and data hold the state and cost almost nothing idle; the app stack holds everything billed by the hour.

(Diagram in the original plan: AWS deployment, VPC across 2 AZs. README.md gets a Mermaid version in phase 17.)

The private subnets reach ECR, CloudWatch and Parameter Store through the NAT gateway, and S3 through the free gateway endpoint. If the instance fails, the ASG launches a replacement in either AZ and it mounts the same EFS file system.

| Stack | Resources | Lifetime |
| --- | --- | --- |
| `kb-network` | VPC `10.0.0.0/16`; public subnets `10.0.1.0/24` and `10.0.2.0/24`; private subnets `10.0.11.0/24` and `10.0.12.0/24`; internet gateway; public and private route tables; S3 gateway endpoint | All week |
| `kb-data` | EFS file system (encrypted, mount target in each private subnet, EFS security group); S3 bucket (versioned, Block Public Access); ECR repository | All week; `DeletionPolicy: Retain` on EFS and S3 |
| `kb-app` | NAT gateway with Elastic IP in public subnet a; private default route to it; ALB, target group (health check `/healthz`), port 80 listener; launch template (`t3.medium`, Amazon Linux 2023, `LabInstanceProfile`, IMDSv2); Auto Scaling group (min 1, max 1, ELB health checks) over both private subnets; CloudFront distribution; CloudWatch alarms; SNS topic | All week if sandbox credit allows; otherwise delete between sessions |

The app stack's NAT gateway, ALB and instance each bill by the hour, while the other two stacks cost almost nothing at this size. Deleting `kb-app` between sessions saves credit, but each recreate brings a new CloudFront domain, so you must rerun `claude mcp add`. Check your remaining sandbox credit before deciding.

Deploy steps:

1. `aws cloudformation deploy` the network stack, then the data stack. Stack outputs (subnet IDs, EFS ID, bucket name, repository URI) are exported for the app stack.
2. Create the token parameter once with the CLI (`aws ssm put-parameter --type SecureString`), since CloudFormation cannot create SecureString parameters.
3. Build the image on your laptop with `docker build --platform linux/amd64`, both fastembed models downloaded into `/models` during the build, then push it to ECR using the sandbox's temporary credentials.
4. Deploy the app stack with an `ImageTag` parameter. User data installs Docker, `amazon-efs-utils` and the CloudWatch agent, mounts EFS at `/data` with TLS, logs in to ECR, runs the container with `--restart unless-stopped`, and installs the nightly backup cron.
5. Seed the corpus from a Session Manager shell: `docker exec kb python -m kb.cli seed seeds/seeds.yaml`.
6. Ship new code by pushing a new tag and updating `ImageTag`. The `AutoScalingRollingUpdate` policy (`MinInstancesInService: 0`) replaces the instance; expect a few minutes of downtime.
7. Tear down by deleting `kb-app` only. EFS and S3 keep the data for the next session.

## Browser extension

A small Chrome Manifest V3 extension sends the page you are looking at to `POST /api/docs`, which covers pages behind a login that `ingest_url` cannot fetch. It captures; it does not crawl.

- **Permissions:** `activeTab`, `scripting`, `storage`, and a host permission for your server URL only.
- **Popup:** title (prefilled), tags, and a Capture button. If text is selected, a "selection only" checkbox sends just that.
- **Capture:** `chrome.scripting.executeScript` returns `document.documentElement.outerHTML` (or the selection's HTML) with the URL. The server runs the same trafilatura pipeline as `ingest_url`.
- **Duplicates:** a `possible_duplicate` response shows the candidates with Update existing and Save anyway buttons.
- **Settings page:** server URL and write token in `chrome.storage.local`.
- **Distribution:** load unpacked from `extension/` for the demo; no Web Store listing.

## Demo corpus and demo script

Seed about 60 public documents that exercise every input format and fit the course theme, plus a few planted cases so dedupe, stale and gaps have something to show.

| Source | Format | Roughly | Shows |
| --- | --- | --- | --- |
| AWS user guide pages for the services in this design (VPC, EC2, Auto Scaling, ELB, EFS, S3, CloudFront, CloudFormation) | URL (HTML) | 30 pages | Boilerplate stripping on a heavy docs site |
| FastAPI docs from its GitHub repo | Markdown | 20 files | Front matter and heading chunking |
| MCP specification pages | URL (HTML) | 5 pages | Lets Claude Code answer questions about itself |
| One AWS whitepaper (e.g. Well-Architected) | PDF | 1 to 2 files | PDF conversion and long-doc chunking |
| Planted: a lightly edited copy of one FastAPI page | Markdown | 1 | Near-duplicate warning |
| Planted: 3 docs with `verified_at` backdated 200 days | any | 3 | `stale` output |

`python -m kb.cli seed seeds/seeds.yaml` reads the manifest, ingests each entry through the normal service layer, then applies the planted cases. It is idempotent, so you can rerun it after a reset.

Demo script (about 15 minutes):

1. Architecture slide: the diagram above, then the three stacks in the CloudFormation console.
2. Web UI: search "how does an Auto Scaling group replace an unhealthy instance" and show the high-confidence hit with its heading path. The knowledge base answers questions about its own architecture.
3. Claude Code: add the remote server, ask "Using the kb, how do I mount EFS on Amazon Linux?" and show the `search` call.
4. Ask Claude to save a short runbook note with `upload`; then upload the planted near-duplicate and show the warning and `merge`.
5. Capture a page with the extension, then find it with search.
6. Ask something outside the corpus, show `low` confidence, then show it in `gaps`.
7. Flip the fallback toggle, repeat a search, and show `mode: keyword` still answering. In the AWS Academy sandbox the instance role cannot publish custom metrics (deviation 8), so the alarm email comes from the load balancer instead. About 20 minutes before the demo, mark the instance unhealthy (`aws autoscaling set-instance-health --instance-id <id> --health-status Unhealthy`): Auto Scaling replaces it (the old one flushes a last snapshot, the new one restores it), `HealthyHostCount` drops to 0 while the replacement boots, and `kb-no-healthy-host` sends its ALARM email, then the OK email. Show those two emails.
8. Failover: terminate the instance in the EC2 console. The ALB marks it unhealthy, the Auto Scaling group launches a replacement (possibly in the other AZ), and the new instance mounts EFS. Search again: same documents, nothing lost. Start this early and talk through steps 9 and 10 while it recovers.
9. Run `stale`, then `verify` one doc.
10. Close on the Well-Architected table and the scale-out path.

## Schedule

Stand up the network and data stacks on Day 1 and the app stack by Day 3, so sandbox restrictions surface while there is still time to work around them.

**Day 1: Wed Oct 7, foundation stacks and skeleton**

- [ ] Deploy `kb-network` and `kb-data`; create the token parameter
- [ ] Repo layout, config, SQLite schema with FTS5 triggers, `/healthz`
- [ ] Dockerfile with both models baked in; confirm fastembed loads the cross-encoder; push to ECR

**Day 2: Thu Oct 8, ingestion**

- [ ] HTML, URL, PDF and Markdown to Markdown; cleanup rules; originals saved to S3
- [ ] Heading chunker with merge and split rules; unit tests on 5 sample docs
- [ ] REST upload, get, delete; exact-hash dedupe

**Day 3: Fri Oct 9, app stack and search**

- [ ] Deploy `kb-app` (NAT gateway, ALB, launch template, ASG, EFS mount in user data); skeleton healthy behind the ALB
- [ ] Embedder, in-memory index, cosine plus FTS5 union, rerank, confidence flag
- [ ] Keyword fallback, admin toggle, gap logging

**Day 4: Sat Oct 10, MCP and write tools**

- [ ] FastMCP tools at `/mcp` (stateless, JSON responses) plus the stdio entry point
- [ ] Token checks, audit log, near-duplicate check, `update`, `merge`, `verify`, `stale`, `gaps`
- [ ] Test end to end from Claude Code, locally and through the ALB

**Day 5: Sun Oct 11, edge, corpus and tuning**

- [ ] CloudFront distribution with cache behaviors and header forwarding; ALB locked to the CloudFront prefix list
- [ ] Seed the corpus; write about 20 test questions; tune confidence and duplicate thresholds
- [ ] Backup cron to S3, a tested restore, and a first failover test

**Day 6: Mon Oct 12, UI, extension and monitoring**

- [ ] Search, doc, add and maintenance pages
- [ ] Chrome extension: popup, capture, settings, duplicate handling
- [ ] CloudWatch agent, log group, metrics, alarms, SNS email subscription

**Day 7: Tue Oct 13, harden and rehearse**

- [ ] SSRF guard, rate limits, nh3 sanitizing, CORS
- [ ] README: architecture diagram, Well-Architected table, cost notes, teardown steps
- [ ] Run the demo script twice, including failover; record a backup video in case the sandbox misbehaves

**Demo: Wed Oct 14**

## Risks, cut lines and open items

The biggest risks are sandbox service restrictions and SQLite on EFS; deploying the foundation stacks on Day 1 and the app stack on Day 3 surfaces both early.

| Risk | Fallback |
| --- | --- |
| Sandbox blocks a service (CloudFront, Session Manager, NAT gateway, `t3.medium`) | Serve the ALB over HTTP; use a bastion host; drop to `t3.small` with the reranker off |
| SQLite misbehaves on EFS (lock errors, slow writes) | Move `/data` to an EBS volume and pin the ASG to one AZ; write up the lost AZ resilience as a trade-off |
| Instance replacement is slow (image pull, model load) | Models baked into the image; measure on Day 5; start the failover step early in the demo |
| CloudFront cuts off MCP requests | Stateless JSON mode avoids long streams; raise the origin response timeout to 60 seconds |
| Sandbox session ends mid-demo | Start the session early; the ASG and restart policy bring the app back; keep the backup video |
| Sandbox credit runs low | Delete `kb-app` between sessions; network and data stacks keep the state |
| fastembed lacks the cross-encoder you need | Use sentence-transformers `CrossEncoder` with CPU-only torch (larger image) |
| PDFs convert poorly (tables, two columns) | Pick demo PDFs that convert cleanly; document the limitation |
| Thresholds feel wrong live | Tune on Day 5; thresholds are environment settings, so no rebuild is needed |

Cut in this order if you fall behind; the first four protect the core demo:

1. Lambda stale and gaps email (stretch goal)
2. Extension selection-only mode
3. Maintenance page in the UI (the MCP tools still cover it)
4. CloudFront (demo over the ALB on HTTP and name the limitation)
5. Reranker (rank by cosine; confidence cutoffs around 0.75 and 0.55 to start)

Never cut: the three CloudFormation stacks, ingestion, semantic search with keyword fallback, all 11 MCP tools, token checks, and the failover demo. Those carry the course grade.

Open items:

- [ ] Which AWS Academy environment will you deploy to: the Learner Lab, or a sandbox inside Cloud Architecting? It decides whether CloudFront, Session Manager and NAT gateways are allowed, and how much credit you have.
- [ ] Confirm Oct 14 as the demo date.
- [ ] Does the instructor expect specific deliverables, such as an AWS Pricing Calculator estimate or a written Well-Architected review? If yes, add them to Day 7.
