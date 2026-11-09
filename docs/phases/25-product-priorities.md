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
| 25.4 Write auth | Implemented and tested in phase 24 (MCP), phases 05 to 16 (REST, UI) | Audit every write route and tool; fill test gaps only | not started |
| 25.5 Backups | 1 day RDS retention, `DeletionPolicy: Delete`, no deletion protection; S3 export nightly | Retention 7, snapshot on delete and replace, protection parameter, teardown script, probe, `docs/RECOVERY.md` | not started |
| 25.6 Alarms | 3 of 7 alarms can never receive data | Conditional custom-metric alarms; ALB, Auto Scaling and RDS alarms; dashboard; runbook | not started |

## Live audit (2026-11-08, read only)

Account `834269865094` (AWS Academy Learner Lab, role `voclabs`), not the Cloud Architecting sandbox account `983926826929` that phases 19 to 24 deployed to.

- **Nothing of kb-mcp is deployed.** No `kb-*` stacks (the last were deleted 2026-10-16 and 2026-10-17), no RDS instances or snapshots, no Auto Scaling groups, load balancers, target groups, instances, NAT gateways, alarms, dashboards, CloudFront distributions or S3 buckets. Two confirmed email subscriptions remain on a `kb-alerts` topic that no longer exists.
- So the deployed database mode, live metrics, target health and AZ spread cannot be verified in this account. Every "deployed" result in `docs/PROGRESS.md` for phases 19 to 24 is historical (account `983926826929`).
- Permissions (IAM policy simulator; `aws:RequestedRegion=us-east-1` must be given, without it the simulator reports false explicit denies for RDS):

| Action | Result |
| --- | --- |
| `rds:CreateDBInstance` (`db.t3.micro`, Multi-AZ, encrypted, 20 GB, postgres) | allowed |
| `rds:CreateDBInstance` with `db.t3.large` | explicit deny |
| `rds:ModifyDBInstance`, `RestoreDBInstanceToPointInTime`, `RestoreDBInstanceFromDBSnapshot`, `CreateDBSnapshot`, `DeleteDBInstance` | allowed |
| `rds:CopyDBSnapshot`, `rds:DeleteDBSnapshot` | implicit deny (a final snapshot cannot be deleted by the lab user) |
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

## 25.4 to 25.6

Filled in as each sub-phase is done.

## Do not

- Deploy, modify or delete anything on AWS without the user's explicit approval.
- Create a restored test database before the user approves the cost.
- Change the default `DatabaseMode`.
- Raise `MaxSize` above 4.

## Acceptance

- [ ] Each sub-phase: relevant tests pass, `cfn-lint` clean for template changes, docs and `docs/PROGRESS.md` updated, one commit.
- [ ] 25.5: restore tested on a non-production copy (needs approval and a deployed `kb-db`).
- [ ] 25.6: each alarm's metric shown to have data on a deployed stack, and one SNS email received (needs a deploy).
