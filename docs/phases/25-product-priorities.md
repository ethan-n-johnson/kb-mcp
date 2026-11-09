# Phase 25: Product priorities (shared search, scale-out, health, auth, backups, alarms)

**Days:** Sun Nov 8 onward · **Depends on:** 24 · **Where:** local first; every AWS change needs the user's approval · **PLAN.md sections:** AWS deployment, Monitoring, Backup and recovery

## Goal

Finish the six product priorities on top of what phases 19 to 24 built, without rebuilding anything that already works:

1. Shared search on RDS PostgreSQL with pgvector.
2. Multi-AZ Auto Scaling, 2 to 4 application servers.
3. A load balancer health check that does not depend on the database.
4. Authentication on every write (MCP, REST, UI).
5. Recoverable database backups and protection against deletion.
6. CloudWatch alarms that actually receive data.

Each priority is a separate sub-phase (25.1 to 25.6) with its own commit and its own row in the `docs/PROGRESS.md` phase log.

## Status

Status words: not started, in progress, implemented (code written), tested (local tests pass), deployed and verified (checked on AWS).

| Sub-phase | Before phase 25 | Phase 25 work | Status |
| --- | --- | --- | --- |
| 25.1 Shared search | Implemented and tested in phases 20 and 21; deployed in account `983926826929` | Pool checks each connection before use (RDS failover) | tested (SQLite legs; the `pg` legs need `KB_TEST_DATABASE_URL`, see 25.1) |
| 25.2 Scale-out | Implemented, tested and deployed in phases 22, 23, 23a | One nightly export per group, not one per instance; single NAT gateway documented | tested (SQLite legs; `pg` legs need `KB_TEST_DATABASE_URL`) |
| 25.3 Health checks | `/healthz` only, and the ALB uses it; it returns 503 when the database is unreachable for 60 s | `/livez` for the ALB, `/healthz` stays the readiness check | tested (SQLite legs; `pg` legs need `KB_TEST_DATABASE_URL`) |
| 25.4 Write auth | Implemented and tested in phase 24 (MCP), phases 05 to 16 (REST, UI) | Audit every write route and tool; fill test gaps only | tested (no code change needed; guard tests added) |
| 25.5 Backups | 1 day RDS retention, `DeletionPolicy: Delete`, no deletion protection; S3 export nightly | Retention 7, snapshot on delete and replace, protection parameter, teardown script, probe, `docs/RECOVERY.md` | tested (templates and scripts with stubs); restore test not run (needs a deployed `kb-db` and approval) |
| 25.6 Alarms | 3 of 7 alarms can never receive data | Conditional custom-metric alarms; ALB, Auto Scaling and RDS alarms; dashboard; runbook | tested (templates); not deployed, metric data and email delivery not verified |

## Live audit (2026-11-08, read only)

Account `834269865094` (AWS Academy Learner Lab, role `voclabs`), not the Cloud Architecting sandbox account `983926826929` that phases 19 to 24 deployed to.

- **Nothing of kb-mcp is deployed.** No `kb-*` stacks (the last were deleted 2026-10-16 and 2026-10-17), no RDS instances or snapshots, no Auto Scaling groups, load balancers, target groups, instances, NAT gateways, alarms, dashboards, CloudFront distributions or S3 buckets. Two confirmed email subscriptions remain on a `kb-alerts` topic that no longer exists.
- So the deployed database mode, live metrics, target health and AZ spread cannot be verified in this account. Every "deployed" result in `docs/PROGRESS.md` for phases 19 to 24 is historical (account `983926826929`).
- Permissions (IAM policy simulator). The context must carry what a real request carries: `aws:RequestedRegion=us-east-1`, and for RDS `rds:Piops=0` and `rds:StorageSize` (the lab policy denies provisioned IOPS and storage above a limit; 40 GB allowed, 200 GB denied). A missing key makes the simulator report a false explicit deny. `infra/probe-rds.sh` section 8 now passes them:

| Action | Result |
| --- | --- |
| `rds:CreateDBInstance` (`db.t3.micro`, Multi-AZ, encrypted, 20 GB, postgres) | allowed |
| `rds:CreateDBInstance` with `db.t3.large` | explicit deny |
| `rds:ModifyDBInstance`, `RestoreDBInstanceToPointInTime`, `RestoreDBInstanceFromDBSnapshot`, `CreateDBSnapshot`, `DeleteDBInstance`, `DeleteDBSnapshot` (gp2, `Piops=0`, 20 GB) | allowed |
| The same with `rds:Piops=1000` or `rds:StorageSize=200` | explicit deny |
| Load balancers, target groups, Auto Scaling, launch templates, NAT gateway, `PutMetricAlarm`, `PutDashboard`, SNS create and subscribe, CloudFront, S3 | allowed |
| `iam:PassRole` | `myS3Role` only |
| `ec2:RunInstances` t3.medium, `myS3Role`, encrypted gp2 30 GB (`--dry-run`) | `DryRunOperation` (would succeed) |
| `myS3Role`: `cloudwatch:PutMetricData`, `logs:PutLogEvents`, `ssm:GetParameter`, `rds-db:connect` | implicit deny (S3 only) |
| ECR, EFS | implicit deny |

- A different Learner Lab account (`711234109184`, in the untracked file `cloudformation describe-stack-events \`, 2026-10-14) denied `elasticloadbalancing:CreateTargetGroup` and `SNS:CreateTopic`. Permissions differ between lab accounts; re-check them with the simulator before each deploy.
- The only copy of the knowledge base on this laptop is `~/.config/kb-mcp/kb-latest.db.gz` (2026-10-14, `integrity_check` ok, 63 docs, 761 chunks, all embedded). The 64 doc, 784 chunk corpus of phases 20 to 24 is not on this machine.
- `~/.config/kb-mcp/up.env` does not set `KB_DATABASE_MODE`, so a deploy from this laptop today runs SQLite with 1 instance.

## 25.1 Shared search on PostgreSQL

**Evidence it exists:** `kb/store/postgres.py` (pool, advisory-lock transactions, schema under its own advisory lock), `kb/store/schema_pg.sql` (`vector(384)`, HNSW, `tsvector` with GIN), `kb/search/candidates.py` (pgvector cosine top-k, `websearch_to_tsquery`), `kb/portable.py` (`import-sqlite`, `export-sqlite`), the `dual` test modules (`test_search.py`, `test_near_duplicate.py`, write paths) and `tests/test_scale_out.py` (two app instances on one database). In PostgreSQL mode no per-instance index exists, so every instance reads the same rows.

**Gap found:** the pool handed out connections without checking them. After an RDS Multi-AZ failover every idle pooled connection is dead, and each would fail one request (500) before the pool replaced it.

**Change:** `check=ConnectionPool.check_connection` on the pool (one round trip per checkout, under a millisecond inside the VPC). The health monitor already reopened its own connection after an error.

**Tests:** `tests/test_postgres_pool.py` (runs everywhere: the pool is built with the check) and `tests/test_postgres_app.py::test_connections_killed_by_a_failover_are_replaced_before_use` (`pg`: fills the pool, kills every client backend with `pg_terminate_backend`, then 2 reads per doc and a search all succeed).

**Default mode decision (user, pending).** SQLite stays the default. Changing it to `postgres` adds the `kb-db` stack (Multi-AZ `db.t3.micro` about 0.034 USD per hour plus 20 GB gp2 Multi-AZ about 4.60 USD per month), a second t3.medium (minimum 2 instances), and 10 to 25 minutes to the first deploy. Migration is automatic (the first boot imports the S3 snapshot into an empty database). Until 25.5 is deployed, deleting `kb-db` destroys the database; only the S3 export and the laptop copy remain.

## 25.2 Multi-AZ Auto Scaling, 2 to 4 instances

**Evidence it exists:** `infra/app.yaml` `Mappings: Capacity` (`postgres`: min 2, desired 2, max 4; `sqlite`: 1/1/1), `VPCZoneIdentifier` with `kb-PrivateSubnetA` and `kb-PrivateSubnetB`, `HealthCheckType: ELB`, `AutoScalingRollingUpdate` (`MinInstancesInService` 1, `MaxBatchSize` 1, `WaitOnResourceSignals`), target tracking on `ASGAverageCPUUtilization` 50, `MetricsCollection` (`GroupInServiceInstances`), deregistration delay 30 s. Shared state in the database: `upload_tickets` and the `settings` row `fallback` (`tests/test_scale_out.py`, two app instances on one database). `tests/test_infra.py` pins the capacity per mode, the policy and the update policy. Historical (account `983926826929`): instances in us-east-1a and us-east-1b, rolling redeploy and failover drill (phase 22), scale-out to 4 and back (phases 23, 23a, 24 run3). Not re-verifiable in account `834269865094` (nothing deployed).

**Gap 1, the nightly export ran once per instance.** The same cron line is on every instance, so 2 to 4 exports wrote the same S3 key at 07:15 UTC. Fix: `export-sqlite --once-per SECONDS`. Inside one write transaction (the advisory write lock serializes the instances) it reads the `settings` row `scheduled_export_at`; if that is younger than the window the command prints `skipped` and exits 0, otherwise it records the time and exports. A failed export puts the previous value back, so a retry is not skipped. Manual exports (`POST /api/admin/export`, `infra/up.sh save`, `export-sqlite` without the flag) are never skipped. The cron uses `--once-per 3600`. A `pg_try_advisory_lock` alone (the open issue's suggestion) was not enough: instance clocks and cron start times differ by seconds, so a second instance could start after the first released the lock and export again.

**Gap 2, one NAT gateway (documented, not changed).** Both private subnets use one route table whose default route goes to one NAT gateway in AZ a. If AZ a fails, instances in AZ b keep serving searches and reads (models on the instance, RDS in the VPC, S3 through the gateway endpoint), but `ingest_url` cannot fetch and no new instance can boot (`dnf install`, `cfn-signal`), so the group cannot replace capacity. The fix is a second NAT gateway in AZ b and a second private route table in `kb-network` (about 33 USD per month more). Left out for sandbox credit; `docs/SECURITY_REVIEW.md` limitation 24.

**Tests:** `tests/test_portable.py` (`pg`: only the first of two connections claims within the window, the next night claims again, a failed export releases the claim, the CLI exports once and then skips, a manual export is never skipped, a failed upload releases the claim; everywhere: `--once-per` must be a positive whole number), `tests/test_infra.py` (the cron line).

## 25.3 Independent health checks

**Before:** the target group checked `/healthz` (interval 15 s, timeout 5 s, unhealthy after 3) and the Auto Scaling group uses `HealthCheckType: ELB`. In PostgreSQL mode `/healthz` answers from the health monitor's cache and returns 503 once the last successful database read is over 60 s old (`kb/health.py`). An RDS Multi-AZ failover commonly takes 60 to 120 s, so every instance would fail the check at the same moment and the group would replace all of them (about 10 minutes each under load, phase 23a), turning a database blip into a full outage.

**Change:**

- `GET /livez` in `kb/main.py`: `{"status": "alive"}`, 200 whenever the process answers HTTP. It reads nothing (no database, cache, search state, model or worker thread). uvicorn accepts connections only after the lifespan has finished, so `/livez` is not 200 before the app has started.
- `infra/app.yaml`: `HealthCheckPath: /livez`; interval, timeout, thresholds and matcher unchanged.
- `/healthz` is unchanged (readiness and diagnostics; still 503 when the database is unreadable or stale). The boot still waits for `/healthz` before `cfn-signal`, so a new instance that cannot reach the database fails its deploy instead of joining. `scripts/demo_check.sh` still reads `/healthz`.

**Trade-off (accepted):** an instance whose own database access is broken while the process lives (for example a wrong security group on one instance) now stays in service and answers 500s instead of being replaced. The boot gate catches it for new instances, and `kb-5xx` (target 5xx) alerts on it. Replacing every instance during a shared outage is the worse failure.

**Tests:** `tests/test_health.py`: both endpoints 200 on a normal app; with the SQLite connection closed `/healthz` is 503 and `/livez` 200; `/livez` works with the store opener patched to fail and the search state removed; `/livez` carries no diagnostics; `pg`: with the monitor's last check 61 s old, and with the monitor unable to reach the database, `/healthz` is 503 and `/livez` 200. `tests/test_infra.py`: the target group uses `/livez` with matcher 200, the group uses ELB health checks, the user data still waits for `/healthz` and never uses `/livez`.

## 25.4 Write authentication

**Audit (each interface read in the code, not taken from the docs):**

| Interface | Writes | Gate | Tests before phase 25 |
| --- | --- | --- | --- |
| MCP over HTTP | `upload`, `upload_file`, `update`, `delete`, `merge`, `ingest_url`, `verify` | `require_http_writer` runs first inside `call_write`, before a connection is opened; missing, wrong, malformed token or an unreadable token store all refuse with one message (fail closed) | `tests/test_mcp.py`: 5 bad header shapes against all 7 tools with docs and audit counts unchanged; valid token works and audits the token name; `upload_file` refused hands out no link; annotations (`readOnlyHint`) match the split; token never logged |
| MCP over stdio | same tools | none by design (local process, `KB_TOKEN` only names the actor) | `tests/test_mcp_stdio.py` |
| REST | the 10 routes in `GATED_BY_TOKEN` | `Depends(require_writer)` (`kb/auth.py`) | 5 routes with bad tokens and unchanged data (`tests/test_write_routes.py`), ingest, the others in their own modules |
| REST upload link | `POST`/`PUT /api/uploads/{ticket}` | the single-use ticket a token holder was issued (SHA-256 stored, one `UPDATE ... RETURNING` in PostgreSQL) | `tests/test_upload_links.py`, `tests/test_scale_out.py` |
| UI | `/ui/add/paste`, `/ui/add/file`, `/ui/add/url`, `/ui/docs/{id}/verify`, `/ui/admin/fallback` | `require_writer(request)` before the service call | `tests/test_ui.py::test_write_buttons_without_a_token_get_401_with_a_message` |

Token handling: `check_token` compares every configured secret with `hmac.compare_digest` and does not stop at the first match; the 401 message is the same for a missing and a wrong token and never echoes the header; tokens come from `KB_WRITE_TOKENS` (local), S3 `config/write-tokens` (instances) or SSM, never from code or images; `tests/test_security.py` and `tests/test_mcp.py` check that no token or `Authorization` header reaches the logs. CloudFront forwards `Authorization` (origin request policy `AllViewerExceptHostHeader` on `/api/*` and `/mcp*`).

**Result:** already correct; nothing in `kb/` changed. The one gap was a guard for future routes: nothing failed if a new state-changing REST route forgot the dependency.

**Tests added (`tests/test_write_routes.py`):** the set of non-GET routes in the OpenAPI schema must equal the classified set (10 token-gated, 2 ticket-gated), so a new write route fails until it is classified; each of the 10 token-gated routes answers 401 to no token, a wrong token and a `Basic` header without echoing them, and the audit log, the doc count, the doc itself and the fallback toggle are unchanged; the upload route with an unknown ticket answers 404 `bad_ticket` and stores nothing.

## 25.5 Backups and recovery

**Before:** `infra/db.yaml` had `BackupRetentionPeriod: 1`, `DeletionProtection: false`, `DeletionPolicy: Delete` and `UpdateReplacePolicy: Delete` (phase 19 chose them because a sandbox reset loses RDS anyway). Deleting `kb-db` destroyed the database and its backups; only the S3 export and the single laptop file remained, and `infra/up.sh save` overwrote that file each time.

**Changes:**

- `infra/db.yaml`: `BackupRetentionDays` parameter (default 7, 1 to 35), `DeletionProtection` parameter (default `true`), `DeletionPolicy: Snapshot` and `UpdateReplacePolicy: Snapshot`, `PreferredBackupWindow` 06:00 to 06:30 UTC (before the 07:15 export), `PreferredMaintenanceWindow` Sunday 08:00 to 08:30 UTC (must not overlap), `CopyTagsToSnapshot`, new export `kb-DbInstanceId` (teardown, restore test, RDS alarms in 25.6).
- `infra/deploy.sh db`: `KB_DB_BACKUP_DAYS`, `KB_DB_DELETION_PROTECTION`.
- `infra/teardown-db.sh` (new): refuses while `kb-app` exists or without a laptop copy younger than 60 minutes that passes `integrity_check`; typed confirmation; protection off, delete (final snapshot), protection back on if the delete fails.
- `infra/up.sh save`: also keeps a dated copy in `~/.config/kb-mcp/saved/` (newest 14), so a bad save never replaces the only good copy.
- `infra/probe-rds.sh` section 8: simulation of modify, snapshot, restore and delete actions with the context a real gp2 request carries, and for a deployed `kb-db` its retention, protection, latest restorable time and snapshots. Run 2026-11-08: all six actions allowed; `kb-db` not deployed.
- `infra/restore-test-db.sh` (new, **not run**): point-in-time copy of `kb-db` into temporary public resources reachable only from the laptop, checked over TLS, then deleted.
- `docs/RECOVERY.md` (new): backup layers with owner, location and survival, RPO and RTO per scenario, teardown and every restore procedure, status.

**Deletion protection and the teardown scripts:** with protection on, `delete-stack` on `kb-db` fails (`DELETE_FAILED`) and leaves the database running. `infra/teardown-db.sh` is now the only documented way to delete it. If someone already ran `delete-stack`, run `infra/teardown-db.sh` afterwards (it turns protection off and deletes again). Updating an existing `kb-db` to this template changes retention and protection in place (`ModifyDBInstance`, no replacement) and changes the deletion policies without touching the instance.

**Sandbox resets** destroy RDS, its snapshots and the bucket; the laptop copy is the only independent recovery copy (`docs/RECOVERY.md`).

**Restore test, needs approval (not done):** requires a deployed `kb-db` (itself a deploy needing approval). Creates one db.t3.micro Single-AZ copy (about 0.017 USD per hour), a temporary subnet group and security group; 20 to 40 minutes; well under 0.10 USD; the script deletes all three afterwards with no final snapshot. `kb-db` is only read.

**Tests (`tests/test_infra.py`):** db template settings, parameters and exports; deploy overrides; dated copies and the 14-copy limit; teardown-db (order of modify and delete, refusal with `kb-app` present, stale, missing or damaged laptop copy, wrong confirmation, protection restored on a failed delete, protection already off, no stack); restore-test (nothing created without confirmation, cleanup never deletes an instance outside its prefix, deletes its own copy without a final snapshot, no write call names the source).

## 25.6 CloudWatch alarms and monitoring

**Audit of the seven alarms before this phase:**

| Alarm | Metric | Gets data in the sandbox? | Decision |
| --- | --- | --- | --- |
| `kb-keyword-mode` | `KbMcp` `KeywordSearches` | No: `MetricsMode=off` by default and `myS3Role` cannot `PutMetricData` (simulated 2026-11-08: implicit deny) | Created only when `MetricsMode` is `api` or `emf` (condition `PublishesMetrics`) |
| `kb-high-latency` | `KbMcp` `LatencyMs` p90 | No, same reason | Same condition; `kb-response-time` (ALB) covers latency in the sandbox |
| `kb-memory` | `CWAgent` `mem_used_percent` | No: the agent needs `PutMetricData` too | Same condition |
| `kb-5xx` | ALB `HTTPCode_Target_5XX_Count` | Yes | Kept |
| `kb-unhealthy-host` | ALB `UnHealthyHostCount` | Yes | Kept (with `/livez` it now means the process is not answering) |
| `kb-no-healthy-host` | ALB `HealthyHostCount`, missing is breaching | Yes | Kept |
| `kb-capacity-max` | `GroupInServiceInstances` (group metrics, postgres) | Yes (went to ALARM in phase 23) | Kept |

**Added, all on AWS-published metrics:** `kb-elb-5xx` (ALB-generated 5xx; phase 23 run1's 76 percent 502s were never counted by `kb-5xx`), `kb-response-time` (ALB `TargetResponseTime` p90 above 6 s for 10 minutes), `kb-cpu` (group average EC2 CPU above 85 percent for 15 minutes), `kb-capacity-short` (metric math, desired minus in-service at least 1 for 15 minutes; group metrics collection is now on in both modes with `GroupDesiredCapacity`), and in postgres mode `kb-db-cpu` (above 80 percent, 15 minutes), `kb-db-connections` (above 60, 10 minutes), `kb-db-storage` (under 2 GB free, 10 minutes), reading the new `kb-DbInstanceId` export. Metrics that AWS publishes continuously use `TreatMissingData: missing`, so silence shows as INSUFFICIENT_DATA rather than OK.

**Dashboards:** `kb-mcp` now reads only AWS metrics (requests and both kinds of 5xx, response time, hosts, desired and in-service instances, CPU); `kb-mcp-db` (postgres) shows RDS; `kb-mcp-search` (`PublishesMetrics`) keeps the custom search and memory widgets.

**Notifications:** unchanged (topic `kb-alerts`, one email subscription that must be confirmed by hand). `docs/ALARMS.md` gives the `set-alarm-state` test for delivery.

**Runbook:** `docs/ALARMS.md` (meaning, cause and response for every alarm, how to check that an alarm has data, status).

**Template size:** `app.yaml` grew to 49,334 bytes; `validate-template --template-body` and `deploy --template-file` without `--s3-bucket` stop at 51,200. New test: every template stays 1 KB below that. Comments in the new alarms were kept short for this reason (open issue: move `deploy.sh` to `--s3-bucket` before the template grows again).

**Tests (`tests/test_infra.py`):** alarm set per database mode and metrics mode (`off`, `api`, `emf`); with the default `MetricsMode` no alarm reads a non-`AWS/` namespace; thresholds and missing-data handling of every alarm; ALB, RDS and EC2 dimensions; RDS alarms read `kb-DbInstanceId` and every resource that imports it is postgres-only; the metric math of `kb-capacity-short` and that both its metrics are collected; dashboards are valid JSON and the main one reads only AWS namespaces; template size.

**Not verified (needs a deploy and approval):** that each alarm's metric has data, alarm state changes, and SNS delivery.

## Do not

- Deploy, modify or delete anything on AWS without the user's explicit approval.
- Create a restored test database before the user approves the cost.
- Change the default `DatabaseMode`.
- Raise `MaxSize` above 4.

## Acceptance

- [ ] Each sub-phase: relevant tests pass, `cfn-lint` clean for template changes, docs and `docs/PROGRESS.md` updated, one commit.
- [ ] 25.5: restore tested on a non-production copy (needs approval and a deployed `kb-db`).
- [ ] 25.6: each alarm's metric shown to have data on a deployed stack, and one SNS email received (needs a deploy).
