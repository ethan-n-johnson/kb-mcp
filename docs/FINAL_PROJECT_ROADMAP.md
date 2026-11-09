# Final project roadmap

Review of kb-mcp after phase 25 and the plan for the remaining phases. Written 2026-11-09 and committed with the phase 25 documentation (commit "docs: phase 25 documentation and final roadmap", on `phase-25-product-priorities`). Phases 26 to 31 are plans: nothing in them has been implemented yet.

## At a glance

| Category | Items |
| --- | --- |
| **Implemented and locally tested** (SQLite run, template tests, stand-in AWS commands) | `/livez` and `/healthz` split; write-route classification and token checks; export claim on SQLite; RDS backup settings, deletion protection, Snapshot policies in `infra/db.yaml`; `teardown-db.sh` and `restore-test-db.sh` logic; alarms and dashboards in `infra/app.yaml`; dated laptop copies |
| **Tested only in a temporary PostgreSQL environment** (PostgreSQL 16.2, pgvector 0.6.2, laptop scratch directory, 2026-11-08; no longer running) | Pool connection check and the failover test; export claim and its release on failure under the advisory lock; `/livez` with a stale health monitor; all 292 PostgreSQL-only test legs. Never run on pgvector 0.8 (CI) |
| **Not yet verified in AWS** (this account) | Everything from phase 25: deploys, `/livez` as the ALB check, alarms receiving data, SNS email, RDS backups, deletion protection, final snapshot, restore, scale-out with the export claim |
| **Recommended future work** | Phases 26 to 31 (section 7); the most urgent is the `infra/up.sh save` fix (section 5, finding 1) |

SQLite is the default database (`DatabaseMode=sqlite`, one instance); PostgreSQL on RDS is optional (`KB_DATABASE_MODE=postgres` in `~/.config/kb-mcp/up.env`, 2 to 4 instances) and meant for a temporary session.

Labels used throughout:

- **Verified**: checked in this review with a command, a test run or by reading the code (the evidence is named).
- **Historical**: recorded in `docs/PROGRESS.md` from earlier phases, mostly in another account. Not re-checked.
- **Assumption**: believed true, not checked.
- **Recommendation**: a proposal that needs approval.

Framing decisions (from the user, unchanged): SQLite stays the default database; PostgreSQL and RDS stay optional and are used only in a temporary session; keep cost low; prefer meaningful AWS demonstrations over a longer service list; nothing is pushed, deployed or changed on AWS without approval.

## 1. Environment and Git state (verified 2026-11-09)

| Item | Value |
| --- | --- |
| Repository | `/home/ajay/CloudComputing/kb-mcp`, remote `origin` = `git@github.com:ethan-n-johnson/kb-mcp.git` |
| Branch | `phase-25-product-priorities` (not pushed at the time of the review; pushed to `origin` with the documentation commit, no pull request) |
| HEAD | `85779b23bb75b20254d9ef05125f782f2a19cf04` |
| Base | `8c76232` (merge of PR #7, phase 24); the branch is 8 commits ahead of `master` |
| Commits | `8bfacf7` 25.1, `aa96625` 25.2, `d7ee66e` 25.3, `d050190` 25.4, `e361d5c` 25.5, `4cf069e` 25.6, `993594d` docs hashes, `85779b2` test fix |
| Uncommitted changes to tracked files | None |
| Untracked | `cloudformation describe-stack-events \` (6,724 bytes, Oct 14) and `cloudwatch describe-alarms \` (959 bytes, Oct 16); kept in place on purpose and never committed |
| Stashes | None |
| Laptop backup | `~/.config/kb-mcp/kb-latest.db.gz`, 2,254,958 bytes, Oct 14 15:48, SHA-256 `6e87ff3c...a43d`; 63 documents, 761 chunks, `integrity_check` ok (checked 2026-11-08). **No `saved/` directory exists**, so there is no second copy |
| Other laptop config | `~/.config/kb-mcp/up.env` and `write-tokens` exist (contents not displayed); no `db-password` |
| Tools | Python 3.14.4 in `.venv` (the image and CI use 3.12); `pytest`, `ruff`, `cfn-lint` in `.venv`; AWS CLI present. **Docker is not usable in WSL** ("could not be found in this WSL 2 distro"; Docker Desktop on Windows has WSL integration off). No PostgreSQL or `shellcheck` on `PATH`; the temporary PostgreSQL 16.2 from the previous session lives in the session scratchpad and is not assumed available |
| AWS | Credentials valid for account `834269865094` (Learner Lab). Read-only checks: no kb stacks (only the lab's own stack), no S3 buckets, no RDS snapshots, no Elastic IPs |

The two untracked files are evidence worth keeping (section 5, new findings 3 and 4). They were read and not changed.

## 2. Completed work (do not redo)

Phase 25 implemented six priorities. Each was verified in this review by its commit and tests; none needs reimplementation. The only defects found are in neighboring code (section 5).

| # | Priority | What exists | Evidence | Live status |
| --- | --- | --- | --- | --- |
| 1 | PostgreSQL shared search | Pool validates connections before handing them out (`check=ConnectionPool.check_connection`), so connections dropped by an RDS failover are replaced | `kb/store/postgres.py`; `tests/test_postgres_pool.py`; failover test in `tests/test_postgres_app.py` fails without the fix (verified 2026-11-08) | Historical only |
| 2 | One export per group | `export-sqlite --once-per 3600` claims a `settings` row inside an advisory-locked transaction; claim released on failure | `kb/portable.py`, `kb/cli.py`, `infra/app.yaml` cron; claim tests in `tests/test_portable.py` | Historical only (scale-out phases 22 to 24) |
| 3 | Independent health checks | `/livez` (no dependencies) for the ALB; `/healthz` (readiness) for `cfn-signal` and `demo_check.sh` | `kb/main.py`, target group `HealthCheckPath: /livez`, `tests/test_health.py` | Not deployed |
| 4 | Write authentication | Every non-GET route is classified as token-gated (10) or ticket-gated (2); MCP write tools require a token over HTTP | `tests/test_write_routes.py`, `tests/test_mcp.py`, `tests/test_ui.py` | Historical (phase 24) |
| 5 | Backups and recovery | RDS: 7-day backups, deletion protection, snapshot on delete or replace, `kb-DbInstanceId` export; `infra/teardown-db.sh`, `infra/restore-test-db.sh`; dated laptop copies on save; `docs/RECOVERY.md` | `infra/db.yaml`, scripts, `tests/test_infra.py` with stand-in `aws` commands | Not deployed; **no restore ever run** |
| 6 | CloudWatch alarms | Alarms read metrics AWS publishes itself (ALB, Auto Scaling, EC2, RDS); custom-metric alarms only when `MetricsMode` is not `off`; dashboards `kb-mcp`, `kb-mcp-db`, `kb-mcp-search` | `infra/app.yaml`, `docs/ALARMS.md`, `tests/test_infra.py` | Not deployed |

Also complete before phase 25 (historical, in `docs/PROGRESS.md`): the application (12 MCP tools, REST, web UI, Chrome extension), both storage backends, S3 snapshots and backups, the four CloudFormation stacks, `infra/up.sh` and teardown scripts, CI workflow, security review, load tests (phases 23 and 23a), demo runbook.

## 3. Current test status

| Run | Result | When | Notes |
| --- | --- | --- | --- |
| `pytest -q` (SQLite, models off) | **1,090 passed, 293 skipped, 22 deselected, 0 failed** | Verified 2026-11-09 | Skips: 292 PostgreSQL legs (no database configured) and 1 `shellcheck` test (not installed here) |
| PostgreSQL legs (`KB_TEST_DATABASE_URL`) | 1,382 passed, 1 skipped, 0 failed | 2026-11-08, local PostgreSQL 16.2 with **pgvector 0.6.2** | Not re-runnable today: no PostgreSQL or Docker available. **Never run on pgvector 0.8 (the CI image)** |
| `pytest -m models` | 22 passed | 2026-11-08 | Real ONNX models |
| `shellcheck` on `infra/*.sh`, `scripts/*.sh` | Clean | 2026-11-08 (0.11.0, installed temporarily) | Not installed now |
| `cfn-lint infra/*.yaml` | Clean | Verified 2026-11-09 | |
| `ruff check` | Clean | Verified 2026-11-09 | |
| `aws cloudformation validate-template` | `db.yaml`, `app.yaml` pass | 2026-11-08 | Read-only call |
| GitHub Actions CI | **Never run on this branch** | | The branch push does not trigger it (new finding 2) |

## 4. Architecture review summary

What the design does well and keeps: one FastAPI service where every interface goes through `kb/service.py`; a storage abstraction that keeps SQLite and PostgreSQL behind one API; private instances behind ALB and CloudFront with no SSH and IMDSv2; tokens outside code, image and user data; failure paths that fall back (keyword search when models fail or are busy, `/livez` independent of the database).

Weaknesses and complexity that matter for the final demo:

| Area | Observation | Consequence |
| --- | --- | --- |
| Two storage modes | Every database change must work twice; 292 tests exist only for PostgreSQL | Acceptable: the dual-backend tests are the mitigation. Do not add a third mode |
| SQLite mode | One instance by design (the database is a file); `InService: 0`, so any stack update that replaces the instance takes the app down for the boot time | Do not update `kb-app` during the demo. Writes in the last 5 seconds before an instance dies can be lost (snapshot interval) |
| Single NAT gateway | In AZ a only; instances in AZ b need it for boot (`dnf`, `cfn-signal`) | An AZ a outage stops new instances from booting. Accepted cost trade-off (`docs/SECURITY_REVIEW.md` limitation 24) |
| Instance role | `myS3Role` is S3 only; no CloudWatch Logs, custom metrics or SSM | Application logs are only on the instance; monitoring relies on AWS-published metrics. Cannot be fixed in the lab |
| Template size | `infra/app.yaml` is 49,334 of 51,200 bytes for inline templates | The next feature that touches the template may not fit |
| Deploy tooling | `infra/up.sh` builds the image with Docker on a fresh account | No Docker in WSL here, so no deploy is possible today |
| Laptop backup handling | `infra/up.sh save` replaces the only good copy first, without checks | See new finding 1 |

## 5. New findings in this review

These were not known before 2026-11-09.

1. **`infra/up.sh save` can destroy the only corpus copy (verified by reading `save_snapshot`, lines 145 to 160).** It downloads the bucket snapshot and moves it over `kb-latest.db.gz`, then makes the dated copy from the new file. The existing Oct 14 file is never copied to `saved/` first, and nothing checks the download (gzip, `integrity_check`, document count). A save after a deploy that started empty, or after a bad import, would silently replace 63 documents with whatever is in the bucket. **P0, fix before any deploy.**
2. **CI does not run on a branch push (verified, `.github/workflows/tests.yml`).** It runs on pushes to `master`, pull requests to `master` and manual `workflow_dispatch`. The branch is now pushed, but that ran nothing; the PostgreSQL legs on pgvector 0.8 need a (draft) pull request or a manual run. Both are outward-facing and need approval.
3. **Account `834269865094` hosted a pre-phase-25 `kb-app` (verified from the untracked file `cloudwatch describe-alarms \`, Oct 16).** It lists six alarms (`kb-5xx`, `kb-high-latency`, `kb-keyword-mode`, `kb-memory`, `kb-no-healthy-host`, `kb-unhealthy-host`) notifying `arn:aws:sns:us-east-1:834269865094:kb-alerts`, all in state OK. This is not in `docs/PROGRESS.md`. It is also supporting evidence for phase 25.6: the three custom-metric alarms (`kb-high-latency`, `kb-keyword-mode`, `kb-memory`) were OK although the role cannot publish their metrics, which is how an alarm without data looks with `notBreaching`. The stacks and the bucket are gone now, so this does not prove the current templates work here.
4. **The other untracked file documents a permission failure (verified).** `cloudformation describe-stack-events \` is from account `711234109184` on Oct 14: `elasticloadbalancing:CreateTargetGroup` and `SNS:CreateTopic` denied, then `ROLLBACK_FAILED` on `NatEip`. That is the reason for a preflight check (phase 27) and for the leftover check after every session.
5. **Untracked files force an image rebuild on every deploy (verified, `infra/push-image.sh` line 37, `infra/up.sh` line 274).** `git status --porcelain` counts untracked files, so the tag becomes `<sha>-dirty` and `up.sh` never reuses an uploaded image. With the two preserved files every deploy rebuilds (about 3 minutes) and uploads a new tarball. Fix: ignore untracked files in that check (`--untracked-files=no`), which keeps the files where they are.
6. **The Claude Code MCP entry `kb` points to a dead deployment (verified).** It targets `https://d30nrx366p5hn0.cloudfront.net/mcp`, which no longer resolves. A new deploy gets a new CloudFront domain, so the entry must be updated after phase 29 (**HUMAN**, Claude Code config).
7. **Option B (several SQLite instances) is not deployable as designed (verified, `Mappings: Capacity`, `sqlite` max 1).** See section 8.

## 6. Remaining-work inventory

Status: **complete**, **needs test**, **needs code**, **needs AWS**, **HUMAN**, **needs approval**.

### 6.1 Testing and verification

| # | Item | Status | Priority |
| --- | --- | --- | --- |
| T1 | PostgreSQL legs on pgvector 0.8 (CI image) | needs approval (PR or manual run) or Docker in WSL | P0 before merge |
| T2 | Export-claim race as a committed test (threads with a barrier, exactly one claim) | needs test (an ad hoc check passed on 2026-11-08: 20 rounds, one winner each) | P1 |
| T3 | `infra/restore-test-db.sh` verification step: the embedded Python has never run. Extract it to a small script taking host, port, user and `sslmode`, and test it against a local PostgreSQL (good copy, empty database, missing `vector` extension) | needs code and test | P1 |
| T4 | `/livez` answers while every worker thread is busy (copy the existing `/healthz` test) | needs test | P1 |
| T5 | Alarm and dashboard metric names and dimensions checked against a table of documented AWS metrics (`AWS/ApplicationELB`, `AWS/AutoScaling`, `AWS/EC2`, `AWS/RDS`, from the CloudWatch metrics pages of each service) | needs test | P1 |
| T6 | `up.sh save`: keeps the previous copy, refuses a corrupt or smaller download (finding 1) | needs code and test | **P0** |
| T7 | A PostgreSQL export restored by the SQLite boot path (the switch back after an RDS session) | needs test | P1 |
| T8 | App startup when `config/database-url` is missing in PostgreSQL mode: clear failure, no empty database | needs test | P2 |
| T9 | Failure scenarios (table 6.6): most have tests; gaps are T6, T8 and the killed-export claim window (documentation only) | partly complete | P1 |
| T10 | Live verification of everything in sections 6.2 and 6.4 | needs AWS | P0 (phase 29) |
| T11 | Real RDS failover and restore | needs AWS and separate approval | P2 (phase 30) |

Only mocked or stubbed so far (verified by reading the tests): S3 via moto; SSM token source via moto; every shell script with stand-in `aws` and `curl`; CloudFormation only linted, validated and parsed; the metrics sender.

### 6.2 Infrastructure and deployment

| # | Item | Status | Priority |
| --- | --- | --- | --- |
| D1 | Docker in WSL (Docker Desktop, Settings, Resources, WSL integration) | **HUMAN, blocker** | P0 |
| D2 | Fresh lab credentials (about 3 hours) per session; SNS confirmation email per new topic | HUMAN | P0 per session |
| D3 | Template size: S3 upload (`aws cloudformation deploy --s3-bucket --s3-prefix templates`, limit 1 MB) for `kb-db` and `kb-app`. Recommended over nested stacks, which need the same S3 upload plus cross-stack wiring and more failure modes. `kb-network` and `kb-data` stay inline (small; the bucket does not exist before `kb-data`) | needs code | P1 |
| D4 | Read-only preflight (`infra/preflight.sh`): credentials, simulated permissions for the actions the stacks need (with the context keys learned in phase 25.5), `RunInstances` dry run, Docker, `up.env`, laptop backup intact | needs code | P1 |
| D5 | Dirty image tag from untracked files (finding 5) | needs code (one flag) | P2 |
| D6 | Leftover check after teardown (`infra/leftovers.sh`, read-only): NAT gateways, Elastic IPs, load balancers, instances, RDS instances and snapshots, stacks in `*_FAILED` | needs code | P1 |
| D7 | Deployment order (network, data, tokens, database when postgres, image, app) | complete (stub tests); live pending | n/a |
| D8 | Cleanup order: `up.sh save`, `teardown-app.sh`, `teardown-db.sh` (postgres only), leftover check. Never delete `kb-network` or `kb-data` | complete (documented); live pending | n/a |
| D9 | Multi-AZ: ALB in two public subnets; Auto Scaling group spans both private subnets; RDS Multi-AZ by default. SQLite mode places its one instance in either AZ | complete in templates; live pending | n/a |
| D10 | `teardown-app.sh` message says the instance uploads a last snapshot (true only in SQLite mode) | small doc fix | P3 |

### 6.3 Security and reliability

Complete (verified in templates and tests, historical live checks): private instances, no public IP, no key pair, no port 22, IMDSv2; ALB reachable only from the CloudFront prefix list; app port only from the ALB; database port only from `kb-db-client`; RDS private and encrypted; bucket private with versioning and SSE; tokens from S3 or SSM, compared in constant time, never logged; every write gated; rate limit, body limit, security headers, SSRF guard.

| # | Item | Status | Priority |
| --- | --- | --- | --- |
| S1 | Bucket policy denying requests without TLS (`aws:SecureTransport`); `infra/data.yaml` has none | needs code | P2 |
| S2 | Secret origin header from CloudFront checked by an ALB listener rule (today any CloudFront distribution can reach the ALB through the prefix list) | needs code; after D3 | P2 |
| S3 | Database password on the `deploy.sh` command line (visible in the laptop process list during the deploy): use a mode-600 parameters file | needs code | P3 |
| S4 | `restore-test-db.sh` copy is public with a laptop /32 rule (temporary, deleted at the end): document in `docs/SECURITY_REVIEW.md` | doc | P3 |
| S5 | `myS3Role` is `s3:*`; IAM is read only | cannot fix (limitation 17) | n/a |
| S6 | CloudFront to ALB is plain HTTP (no domain or certificate in the lab); S2 compensates | cannot fix cheaply | n/a |
| S7 | Laptop backup safety (finding 1) | needs code | **P0** |
| S8 | Observability: logs only on the instance; failed boots write `s3://<bucket>/debug/` | accepted limitation | n/a |

### 6.4 Demonstration readiness

| AWS service | Role | Option A (SQLite) | Needs PostgreSQL | Ongoing cost while deployed | Delete after demo | Evidence to collect |
| --- | --- | --- | --- | --- | --- | --- |
| CloudFront | HTTPS entry, static caching | Yes | No | Free tier at demo traffic | With `kb-app` | `x-cache: Hit` on `/static/`, `Miss` on `/api/search` |
| ALB | Health checks on `/livez`, two AZs | Yes | No | $0.0225/h + LCU + 2 public IPv4 at $0.005/h | With `kb-app` | Target health page; direct ALB request refused |
| EC2 + Auto Scaling | Private instance, self-healing | Yes (1 instance) | Scale-out to 2 to 4 | t3.medium $0.0416/h + 30 GB gp2 | With `kb-app` | Replacement drill timeline |
| VPC, subnets, IGW, route tables, S3 endpoint | Network | Yes | No | Free | Never (`kb-network`) | Resource map |
| NAT gateway | Outbound for boot | Yes | No | **$0.045/h + $0.045/GB + $0.005/h IPv4** | With `kb-app` | Private route table |
| S3 | Image, snapshots, backups, config | Yes | No | Cents | Never (`kb-data`); lifecycle rules expire old objects | Snapshot object versions |
| CloudFormation | All infrastructure | Yes | `kb-db` | Free | `kb-app` (and `kb-db`) | Stacks, outputs, exports |
| CloudWatch | Alarms and dashboards on AWS metrics | 7 alarms, 1 dashboard | 4 more alarms, `kb-mcp-db` | About $0.10 per alarm-month, prorated; 3 dashboards free | With `kb-app` | Dashboard during load; alarm history |
| SNS | Alarm email | Yes | No | Free tier | With `kb-app` | Received ALARM and OK emails |
| IAM | Existing `myS3Role` | Yes | No | Free | n/a | Explain the lab restriction |
| RDS PostgreSQL | Shared database, backups | No | Yes | $0.036/h Multi-AZ ($0.018 Single-AZ) + storage | `kb-db` (final snapshot stays and bills until deleted) | Multi-AZ, backups, protection, restore output |

Optional, free, no new deployment: **CloudTrail event history** (allowed in the lab) to show the API calls a deploy made. Not recommended: Route 53 (no domain), Lambda or EventBridge (EventBridge not listed), Secrets Manager (not listed), ECR and EFS (denied).

### 6.5 Documentation and presentation

| # | Item | Status | Priority |
| --- | --- | --- | --- |
| P1 | README diagrams: add `/livez`, the AWS-metric alarms, SQLite as the default path | needs update | P1 |
| P2 | `docs/SETUP.md`: Docker in WSL, preflight, leftover check | needs update | P1 |
| P3 | README cost notes: missing public IPv4, SQLite-only total, alarm cost, final RDS snapshots | needs update | P1 |
| P4 | `docs/DEMO.md` is written for the 4-instance PostgreSQL class session; needs a SQLite demo sequence with a fallback per step and the optional PostgreSQL segment | needs update | P1 |
| P5 | `docs/RECOVERY.md`, `docs/ALARMS.md` status tables after phases 29 and 30 | after AWS phases | P1 |
| P6 | One consolidated AWS Academy limitations table (today spread over PROGRESS, SECURITY_REVIEW, README) | missing | P2 |
| P7 | `docs/PROGRESS.md`: record finding 3 (earlier `kb-app` in this account) | missing | P3 |
| P8 | Phase 24 open acceptance items: extension tested in Chrome, final deck, CI green, GitHub matches the laptop (`docs/phases/24-final-wrap.md`) | HUMAN, teammate coordination | P1 |
| P9 | Presentation: architecture slide, one slide per service with its role, demo script, failure and monitoring demo, security demo, cost and cleanup slide | missing | P1 |

### 6.6 Failure scenarios

| Scenario | Expected | Covered by | Gap |
| --- | --- | --- | --- |
| S3 unavailable at boot | Boot retries, then fails rather than starting empty | `tests/test_backup.py`, `tests/test_portable.py`, user data tests | None |
| S3 unavailable at runtime (SQLite) | Writes succeed, snapshot retried | `tests/test_snapshot.py` | None |
| Database unavailable (PostgreSQL) | `/healthz` 503, `/livez` 200, no instance replaced | `tests/test_health.py` (25.3) | Live (phase 30) |
| Connection loss (failover) | Pool replaces dropped connections | `tests/test_postgres_app.py` (25.1) | Live (phase 30) |
| Instance terminated or stuck | ALB marks it unhealthy, group replaces it, SQLite restores the snapshot | Template tests; historical drills | Live (phase 29) |
| Corrupt backup | SQLite boot refuses it and uses an older one; `teardown-db.sh` refuses a damaged laptop copy | Tests | **`up.sh save` (T6)** |
| Invalid token | 401 or tool error, nothing written | Every write path | None |
| Token store unreadable | Writes refused | Tests | None |
| Missing configuration (`config/database-url`) | Clear boot failure | Not tested at app level | T8 |
| Export killed after claiming | Retry blocked up to an hour, then the claim expires | By design | Document |

## 7. Phase roadmap

Order: protect the data first, then close local test gaps, then make deployments fail early and clean up fully, then (optionally) security, then one live SQLite session, then (optionally) one PostgreSQL session, then documents. The previously proposed structure was kept where the review supports it, with two changes: the data-safety fix now leads phase 26, and running CI moved into phase 27 next to the other deploy-readiness work.

### Phase 26: Data safety and local test gaps

- **Objective:** no command can lose the corpus, and every phase 25 behavior has a committed test.
- **Why:** finding 1 is the only known risk of irreversible data loss; the test gaps are in code that will be deployed next.
- **Files:** `infra/up.sh`, `infra/restore-test-db.sh`, a new `infra/verify_restore.py` (or a module under `kb/`), `tests/test_infra.py`, `tests/test_portable.py`, `tests/test_health.py`, `tests/test_backup.py`, `docs/RECOVERY.md`, `docs/phases/26-*.md`, `docs/PROGRESS.md`.
- **Changes:** `up.sh save` copies the current `kb-latest.db.gz` into `saved/` before anything else, downloads to a temporary file, checks gzip, `integrity_check` and document count, and refuses (keeping the old file) when the check fails or the count drops, unless `--force`. T2, T3, T4, T5, T7, T8. One sentence on the claim window.
- **Tests:** a stand-in test that a failed or smaller download leaves the old file byte-identical and preserved in `saved/`; the new tests above, each shown to fail with its fix reverted; full SQLite run; PostgreSQL legs (needs D1 or the scratchpad PostgreSQL); `shellcheck`.
- **Docs:** RECOVERY (save checks), phase file, PROGRESS row.
- **Dependencies:** none. **Risks:** the save path touches the only backup: tests must use temporary directories, never `~/.config/kb-mcp`. **Complexity:** medium. **Priority:** P0 (save) and P1 (tests).
- **Done when:** all tests pass on SQLite and PostgreSQL; the real `~/.config/kb-mcp` is untouched (checksum unchanged).
- **AWS or cost:** none. **Approval:** yes, to start (local code).

### Phase 27: Deployment readiness

- **Objective:** a deploy in a fresh lab account succeeds or stops early with a clear reason, and cleanup is provably complete.
- **Why:** the only recorded failure in a lab account was a permission denial halfway through (finding 4); template headroom is about 840 bytes before the size test fails; the dirty tag forces rebuilds.
- **Files:** `infra/deploy.sh`, `infra/up.sh`, `infra/push-image.sh`, new `infra/preflight.sh` and `infra/leftovers.sh`, `infra/teardown-app.sh`, `tests/test_infra.py`, `docs/SETUP.md`, README.
- **Changes:** D3 (S3 template upload and `validate-template --template-url`), D4, D5, D6, D10. Raise the template size test only after S3 upload works.
- **Tests:** stand-in tests for the new scripts and flags; `cfn-lint`; `shellcheck`.
- **CI (approval needed separately):** push the branch and open a draft pull request, or run the workflow by hand, so the PostgreSQL legs run on pgvector 0.8 (T1).
- **Docs:** SETUP (Docker in WSL, preflight, leftovers), README, phase file, PROGRESS.
- **Dependencies:** none (may run alongside 26). **Risks:** a wrong S3 upload flow breaks every deploy; phase 29 is its first real use. **Complexity:** medium. **Priority:** P1 (T1 P0).
- **Done when:** tests pass; the preflight passes read-only against this account; CI green (if approved).
- **AWS or cost:** read-only calls only; a push and PR are outward-facing. **Approval:** yes (code), and separately for the push and PR.

### Phase 28: Security hardening (optional)

- **Objective:** close the two cheapest real gaps.
- **Why:** both are real improvements for free; neither is needed for the demo to run.
- **Files:** `infra/data.yaml`, `infra/app.yaml`, `infra/deploy.sh`, `infra/up.sh`, `tests/test_infra.py`, `tests/test_infra_security.py`, `docs/SECURITY_REVIEW.md`.
- **Changes:** S1, S2 (secret generated on the laptop like the tokens, never printed), S3, S4.
- **Tests:** template tests (policy statement, listener rule, secret never in outputs).
- **Dependencies:** 27 (template size). **Risks:** a wrong listener rule blocks all traffic; a wrong bucket policy blocks the instances' S3 access. Both verified in phase 29. **Complexity:** medium. **Priority:** P2.
- **Done when:** tests pass; SECURITY_REVIEW lists the checks to run in phase 29.
- **AWS or cost:** none. **Approval:** yes, including whether it is in scope.

### Phase 29: Live SQLite deployment and verification

- **Objective:** prove the default architecture in this account and collect the presentation evidence.
- **Why:** nothing has been verified live in this account since phase 25; the demo depends on it.
- **Steps:** preflight; `infra/up.sh` (SQLite); confirm the SNS email (HUMAN); `scripts/demo_check.sh`; SECURITY_REVIEW verification commands; each alarm's metric has data; `set-alarm-state` email test; instance replacement drill (`set-instance-health Unhealthy`), timed, with emails; CloudFront cache headers; writes with and without a token; update the Claude Code MCP entry (finding 6, HUMAN); screenshots; `infra/up.sh save` (with the phase 26 checks); `teardown-app.sh`; leftover check.
- **Files:** `docs/PROGRESS.md`, phase file, ALARMS and RECOVERY status tables. Code only for defects the deploy reveals, each with a test and its own commit.
- **Dependencies:** 26, 27 (28 if in scope); D1, D2. **Risks:** permissions differ from the simulator; a failed rollback leaves an Elastic IP. **Complexity:** medium. **Priority:** P0.
- **Done when:** demo check passes; every SQLite-mode alarm shows data or a reasoned no-traffic state; one alarm email received; replacement timed; afterwards only `kb-network` and `kb-data` remain.
- **AWS or cost:** creates `kb-network`, `kb-data`, `kb-app`; about $0.13 per hour, about $0.40 for a 3-hour session. **Approval:** yes, with a date.

### Phase 30: Temporary PostgreSQL session (optional)

- **Objective:** demonstrate RDS, shared storage across 2 to 4 instances in two AZs, backups and recovery without changing the default.
- **Steps:** `KB_DATABASE_MODE=postgres` for this session only; `infra/up.sh` (creates `kb-db`, imports the laptop copy); two instances in two AZs; `infra/class-capacity.sh up` to 4 and back, or load to trigger CPU scaling; RDS alarms and dashboard; optional forced failover and optional `restore-test-db.sh` (each separately approved); `up.sh save`; `teardown-app.sh`; `teardown-db.sh`; decide about the final snapshot; switch back; next SQLite deploy restores the export (T7 tested first).
- **Dependencies:** 29, T7. **Risks:** a forgotten `kb-db` bills about $0.04 per hour; final snapshots stay; data loss at the switch back is covered by T6 and T7. **Complexity:** high. **Priority:** P2.
- **Done when:** scale-out and back recorded; RDS alarms show data; restore check (if approved) prints `RESTORE CHECK PASSED` and cleans up; both stacks deleted; SQLite mode back with the same document count.
- **AWS or cost:** about $0.22 per hour (2 instances, Multi-AZ), about $0.66 for 3 hours, under $0.10 for the restore test, plus snapshot storage if kept. **Approval:** yes, per step.

### Phase 31: Final documentation and presentation

- **Objective:** documents and slides say only what was verified.
- **Changes:** P1 to P9; this roadmap marked done; every README and DEMO claim linked to a PROGRESS entry.
- **Dependencies:** 29 (and 30 if run). **Risks:** claims outrunning evidence. **Complexity:** low to medium. **Priority:** P1.
- **Done when:** the completion checklist (section 11) is all ticked. **AWS or cost:** none. **Approval:** yes, to start.

**Estimate:** 6 phases (26, 27, 29 and 31 required; 28 and 30 optional), 7 to 10 commits.

## 8. AWS configuration comparison

| | Option A: SQLite | Option B: several instances with SQLite | Option C: temporary PostgreSQL |
| --- | --- | --- | --- |
| Deployable as built | Yes in the templates (`sqlite` capacity 1/1/1); live unverified in this account | **No.** `Capacity` caps SQLite at 1, and correctly: each instance would hold its own database file, writes would diverge, and the S3 snapshots would overwrite each other (last writer wins). Making it work needs a shared store, which is what Option C is | Yes in the templates (`postgres` 2/2/4); historical deployments in another account; needs `kb-db` |
| What it shows | CloudFront, ALB health checks, private EC2 behind NAT, Auto Scaling self-healing with S3 restore, CloudFormation, CloudWatch alarms and dashboard, SNS, S3 lifecycle and versioning | Within Option A: the ALB and the group span two AZs and the single replacement instance may start in either; the reason SQLite cannot scale out is a good architecture talking point | Everything in A plus 2 to 4 instances across two AZs sharing one database, scale-out on CPU, RDS Multi-AZ, automated backups, deletion protection, final snapshot, restore |
| Cost per hour (estimate) | About $0.13 | Same as A | About $0.22 (2 instances), $0.31 (4) |
| Time to deploy | About 25 minutes on a fresh account | n/a | 40 to 55 minutes |
| Cleanup | `up.sh save`, `teardown-app.sh` | n/a | Plus `teardown-db.sh`, final snapshot decision, switch back to SQLite |
| Risk | Lowest | n/a | Higher: more steps, more cost if forgotten, data switch back |

**Recommendation:** run Option A as the main demonstration, and explain Option B's limit on one slide rather than building it. Add Option C as a short, separate session only if the presentation needs live scale-out and RDS; otherwise present the phase 22 to 24 load-test evidence, clearly labeled as from the teammate's account. A gives the best value per dollar; C adds the two things A cannot show (shared state across instances and managed database recovery).

Nothing here is confirmed deployable in this account until the phase 27 preflight passes and phase 29 runs.

## 9. Cost and cleanup

Prices: us-east-1 on-demand list prices from the README cost notes (checked 2026-11-07) plus public IPv4 at $0.005 per address per hour (assumption: current AWS pricing, verify before relying on it). Lab credit may be counted differently.

| Option | Per hour | 3-hour session | Forgotten for 24 hours |
| --- | --- | --- | --- |
| A, SQLite | about $0.13 | about $0.40 | about $3.10 |
| C, PostgreSQL, 2 instances, Multi-AZ | about $0.22 | about $0.66 | about $5.30 |
| C with Single-AZ RDS | about $0.19 | about $0.57 | about $4.60 |
| RDS restore test (db.t3.micro, 20 to 40 minutes) | about $0.02 | under $0.10 total | script deletes it |

Resources that keep billing until deleted: NAT gateway and its Elastic IP, ALB and its public addresses, EC2 instances, RDS instance, RDS final snapshots (stay after `kb-db` is deleted), and an Elastic IP left by a failed rollback (seen once, finding 4). Free or negligible: VPC, S3 at this size, dashboards, SNS at this volume, alarms (cents).

Required cleanup after each session: `infra/up.sh save`; `infra/teardown-app.sh`; `infra/teardown-db.sh` in PostgreSQL sessions; then a read-only check for NAT gateways, Elastic IPs, load balancers, RDS instances and snapshots, and failed stacks (D6). Never delete `kb-network` or `kb-data`.

## 10. Risks and dependencies

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Corpus lost through `up.sh save` | Medium once deploys start | Severe, irreversible | Phase 26 first; until then, never run `save`, and consider a manual dated copy of the backup (approval) |
| No Docker, so no deploy | Certain today | Blocks 29 and 30 | D1 (HUMAN) |
| Lab permissions differ from the simulator | Medium | A failed deploy, leftovers | Preflight (D4), leftover check (D6) |
| pgvector 0.8 behaves differently from 0.6.2 | Low | PostgreSQL mode broken | T1 before merge or phase 30 |
| Template too large after one more change | High if templates grow | Deploy refused | D3 before phase 28 |
| Credentials expire mid-deploy | Medium | Half-finished stack | Start each AWS phase with fresh credentials; the scripts stop on `ExpiredToken` |
| Forgotten resources | Medium | Cost | D6 after every session |
| Teammate's repository diverges | Medium | Merge conflicts | Coordinate before pushing (P8) |

Dependencies: 26 and 27 have none; 28 needs 27; 29 needs 26, 27, D1 and D2; 30 needs 29 and T7; 31 needs 29.

## 11. Final project completion checklist

- [ ] Phase 26: save path safe; T2 to T8 committed and passing
- [ ] Phase 27: preflight, S3 template upload, leftover check, clean image tag
- [ ] CI green on the branch, including PostgreSQL legs on pgvector 0.8 (approval for push and PR)
- [ ] Phase 28 done or explicitly out of scope
- [ ] Docker in WSL enabled (HUMAN)
- [ ] Phase 29: live SQLite deployment verified, evidence collected, torn down, no leftovers
- [ ] Laptop backup still intact (63 documents) after the live session, plus a dated copy
- [ ] Phase 30 done or explicitly skipped (with the historical evidence labeled)
- [ ] Claude Code MCP entry updated or removed (HUMAN)
- [ ] Phase 24 leftovers: extension tested, deck, GitHub matches the laptop (teammate)
- [ ] README, SETUP, DEMO, RECOVERY, ALARMS, SECURITY_REVIEW match the verified state
- [ ] Presentation and demo script with fallbacks rehearsed
- [ ] No kb resources billing in the account except `kb-network` and `kb-data`

## 12. Decisions needed

1. Approve phase 26 (local), starting with the save fix.
2. Optionally approve a one-time manual dated copy of `~/.config/kb-mcp/kb-latest.db.gz` (a copy only; the original is not touched).
3. Enable Docker Desktop WSL integration (HUMAN).
4. Approve phase 27, and separately a draft pull request or a manual workflow run so CI runs (the branch push itself is done).
5. Phase 28 in or out of scope.
6. Approve phase 29 and pick a date with fresh credentials.
7. Phase 30 in or out; separately, the forced failover and the RDS restore test; and whether to keep final RDS snapshots.
8. Done 2026-11-09: this roadmap is committed, and findings 1 to 6 are recorded under "Open issues" in `docs/PROGRESS.md`.
