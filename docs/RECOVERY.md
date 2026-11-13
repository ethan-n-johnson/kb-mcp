# Backup and recovery

How the knowledge base is backed up, what each copy protects against, and how to restore it. Written in phase 25.5 (2026-11-08). The restore procedures for RDS are written and scripted, **but no RDS restore has been run yet**; see "Status" at the end.

## Where the data lives

| Mode | Authoritative copy | Notes |
| --- | --- | --- |
| `postgres` | RDS PostgreSQL instance of the `kb-db` stack (Multi-AZ, private subnets) | Shared by the 2 to 4 app instances |
| `sqlite` | `/data/kb.db` on the single instance's root volume | Snapshotted to S3 after every write (5 s batching) |

Everything else (models, image, code) can be rebuilt; only the documents, chunks, embeddings, tags, audit log and gaps need recovery.

## Backup layers

| Layer | Where | Owner (who makes it) | When | Kept | Survives `kb-db` deletion | Survives a sandbox reset |
| --- | --- | --- | --- | --- | --- | --- |
| RDS automated backups (point-in-time restore) | RDS, same account and region | AWS, from `BackupRetentionPeriod` in `infra/db.yaml` | Continuous (transaction logs every 5 minutes) plus a daily snapshot in 06:00 to 06:30 UTC | 7 days (`BackupRetentionDays`) | No: deleted with the instance | No |
| Final RDS snapshot | RDS manual snapshots | CloudFormation (`DeletionPolicy: Snapshot`, `UpdateReplacePolicy: Snapshot`) | When the stack deletes or replaces the instance | Until deleted by hand | Yes | No |
| S3 export (`snapshots/kb-latest.db.gz`, portable SQLite, gzip) | `kb-data` bucket, versioned, SSE AES256 | The app: nightly cron `export-sqlite --once-per 3600` (07:15 UTC, one instance per group), `POST /api/admin/export`, `infra/up.sh save` | Nightly and on demand | Current version plus 30 days of noncurrent versions (bucket lifecycle) | Yes | **No**: a reset gives a new, empty account |
| Laptop copy (`~/.config/kb-mcp/kb-latest.db.gz` plus `~/.config/kb-mcp/saved/kb-<UTC time>.db.gz`) | The operator's laptop, outside the repo | The operator: `infra/up.sh save` | Before every session end and before class | The newest 14 dated copies | Yes | **Yes: the only copy that does** |
| `sqlite` mode: S3 snapshot plus nightly `backups/` | `kb-data` bucket | The app (`kb/snapshot.py`, cron `backup`) | After every write, nightly | 30 days | n/a | No |

**AWS Academy sandbox resets.** A timed-out or reset lab session can come back as a new, empty account (seen twice, 2026-10-09). That deletes every stack, the RDS instance, its automated backups, every RDS snapshot and the S3 bucket with all its versions. Only the laptop copy survives. Treat `infra/up.sh save` as part of ending every session, and keep a second copy of `~/.config/kb-mcp/saved/` somewhere else (another disk or cloud drive) if the data matters. Since phase 26 a save checks the download before it replaces the laptop copy (below).

## How `infra/up.sh save` protects the laptop copy (phase 26)

Found 2026-11-09 and fixed in phase 26: `save` used to move the download over `kb-latest.db.gz` before checking it. Now, in this order:

1. Before anything else (even the credentials check), the current `kb-latest.db.gz` is copied to `saved/kb-<UTC time>-before.db.gz`, unless an identical copy is already in `saved/`.
2. The bucket snapshot is downloaded to `kb-latest.db.gz.tmp` and checked with `infra/scripts/check_sqlite_copy.py` (standard library Python): readable gzip, `PRAGMA integrity_check` is `ok`, the `docs` and `chunks` tables exist.
3. The document count must be at least the current copy's. A smaller count stops the save with the two numbers; `infra/up.sh save --force` accepts it when the deletions were intended. A damaged download is refused even with `--force`.
4. Only then does the download replace `kb-latest.db.gz` and get its own dated copy. On any refusal the old file is byte-identical and the temporary file is deleted.

If the current laptop copy is itself damaged, the save says so and any intact download replaces it (the damaged file is still kept in `saved/`). Keeping a second copy of `saved/` on another disk or cloud drive is still recommended: the laptop is the only copy that survives a sandbox reset.

**The scheduled export claim window.** With `--once-per 3600` the instance that claims the nightly export records the time first; if that instance is killed after claiming and before the upload finishes (a failed upload releases the claim, a killed process cannot), no other instance runs the scheduled export until the hour has passed, so that night has no export. `POST /api/admin/export` and `infra/up.sh save` are never blocked by the claim.

## Recovery objectives

| Scenario | RPO (data that can be lost) | RTO (time to serve again) | Path |
| --- | --- | --- | --- |
| RDS primary fails (Multi-AZ) | 0 (synchronous standby) | 1 to 2 minutes of failover, plus seconds for the pool to replace dead connections (phase 25.1); the ALB keeps the instances (phase 25.3) | Automatic |
| Bad write or delete through the app, noticed within 7 days | Up to 5 minutes before the mistake | 30 to 60 minutes (restore, point the app at it, roll the instances; estimated, not measured) | "Point-in-time restore" |
| Bad write noticed later than 7 days | Back to the newest good S3 version or laptop copy | About 30 minutes | "Restore from an export" |
| `kb-db` deleted deliberately | 0 if `infra/up.sh save` ran right before (the teardown script insists) | One `infra/up.sh` run, 30 to 55 minutes in a new account | "Planned teardown and rebuild" |
| `kb-db` deleted by mistake | Blocked: deletion protection makes the delete fail. If protection was off: 0 to the final snapshot | About 30 minutes from the final snapshot | "Restore from the final snapshot" |
| Sandbox reset | Back to the last laptop copy | One `infra/up.sh` run, 30 to 55 minutes | "Planned teardown and rebuild" (from the laptop copy) |

## Deletion protection and teardown

`infra/db.yaml` sets `DeletionProtection: true` by default. While it is on, `aws cloudformation delete-stack --stack-name kb-db` fails with `DELETE_FAILED` and the database keeps running. That is the point: a stray delete cannot drop the data. The cost is that a deliberate delete needs one extra step, which `infra/teardown-db.sh` does:

1. `infra/up.sh save` while `kb-app` is still up (the app exports RDS to S3, the laptop downloads it and keeps a dated copy).
2. `infra/teardown-app.sh` (`kb-app` imports `kb-db`'s exports, so it must go first).
3. `infra/teardown-db.sh`: refuses while `kb-app` exists, refuses unless the laptop copy is younger than 60 minutes and passes `integrity_check`, asks you to type `kb-db`, turns deletion protection off, deletes the stack (CloudFormation takes the final snapshot first), and turns protection back on if the delete fails.

Turn protection off for the stack itself only for a reason (`KB_DB_DELETION_PROTECTION=false` in `~/.config/kb-mcp/up.env`, then `infra/up.sh`).

Final snapshots stay until you delete them (`aws rds delete-db-snapshot --db-snapshot-identifier <id>`, allowed in the lab as of 2026-11-08). Each costs snapshot storage for the data it holds (the corpus is tens of MB, so cents per month).

## Procedures

### Planned teardown and rebuild

```bash
infra/up.sh save          # export and download (postgres mode asks the app first)
infra/teardown-app.sh
infra/teardown-db.sh
# later, same or new account:
infra/up.sh               # recreates kb-db empty; the first instance imports the saved copy (import-sqlite --if-empty)
```

Check: `scripts/demo_check.sh "https://<CloudFront domain>"` and compare `/healthz` `chunks` with the count `teardown-db.sh` printed for the laptop copy.

### Restore from an export (S3 version or laptop copy)

Use this to go back to a known good export (for example after a bad bulk delete). `import-sqlite` only writes into an empty database unless `--force` is given, and `--force` replaces everything.

1. Find the export: a laptop file in `~/.config/kb-mcp/saved/`, or an older S3 version (`aws s3api list-object-versions --bucket <bucket> --prefix snapshots/kb-latest.db.gz`).
2. Upload it as the snapshot key if it is not already there (`aws s3 cp <file> s3://<bucket>/snapshots/kb-latest.db.gz`).
3. Rebuild the database from it. With no SSM or SSH on the instances (deviation 8), the dependable way is the teardown and rebuild above: delete `kb-db` (the final snapshot keeps the bad state too, in case it is wanted) and run `infra/up.sh`, whose first instance imports the snapshot into the new, empty database.

### Point-in-time restore (bad write noticed within 7 days)

AWS restores to a **new** instance; the running one is never overwritten.

1. Note the time just before the mistake (UTC). The audit log (`GET /api/audit`) shows when each write happened.
2. Restore into the stack's private subnet group and security group, so the app instances can reach the copy:

   ```bash
   SRC=$(aws cloudformation list-exports --query "Exports[?Name=='kb-DbInstanceId'].Value" --output text)
   aws rds describe-db-instances --db-instance-identifier "$SRC" \
     --query 'DBInstances[0].[DBSubnetGroup.DBSubnetGroupName,VpcSecurityGroups[0].VpcSecurityGroupId]' --output text
   aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier "$SRC" \
     --target-db-instance-identifier kb-db-pitr-<yyyymmddhhmm> --restore-time <UTC time> \
     --db-subnet-group-name <subnet group> --vpc-security-group-ids <DbSg id> \
     --db-instance-class db.t3.micro --storage-type gp2
   aws rds wait db-instance-available --db-instance-identifier kb-db-pitr-<yyyymmddhhmm>
   ```

3. Point the app at the copy: write a new `config/database-url` object (same user, password and database, the copy's endpoint) without printing it, then replace the instances so each reads it at start (`aws autoscaling start-instance-refresh --auto-scaling-group-name <AsgName>`).
4. Check `/healthz` counts and the documents the mistake touched.
5. Make it durable: `infra/up.sh save` exports the restored data to S3 and the laptop. The copy is outside CloudFormation; to return to a stack-managed database, run the planned teardown and rebuild (the rebuild imports that export) and then delete the copy (`aws rds delete-db-instance --db-instance-identifier kb-db-pitr-... --skip-final-snapshot`).

### Restore from the final snapshot

`aws rds restore-db-instance-from-db-snapshot --db-instance-identifier kb-db-snap-<date> --db-snapshot-identifier <final snapshot> --db-subnet-group-name ... --vpc-security-group-ids ...`, then steps 3 to 5 of the point-in-time restore. If `kb-db` no longer exists, recreate it first (`infra/up.sh` stops before the app if you only need the network, subnet group and security groups; or deploy `infra/deploy.sh db`) and use its subnet group and `DbSg`.

### Restore test (non-production)

`infra/restore-test-db.sh` proves the automated backups restore, without touching `kb-db`:

- Reads `kb-db`'s identifier and latest restorable time (nothing else is done to it).
- Creates `kb-restore-test-<time>`: db.t3.micro, Single-AZ, gp2, restored to the latest restorable time, in a temporary DB subnet group over the public subnets, with a temporary security group that allows 5432 from the laptop's address only.
- Connects from the laptop (TLS) and checks the doc, chunk and embedded counts, that pgvector is installed, and that a cosine query runs.
- Deletes the copy (no final snapshot), the subnet group and the security group, also on failure (`--keep` leaves them; `--cleanup` removes leftovers).
- Prints the time from start to available and to checked (the measured part of the RTO).

Cost: about 0.017 USD per hour for the instance plus a few cents of storage; 20 to 40 minutes in all, so well under 0.10 USD. Needs a deployed `kb-db` and the user's approval before it is run.

## Status

| Item | State |
| --- | --- |
| 7 day retention, Snapshot policies, deletion protection, backup and maintenance windows, `kb-DbInstanceId` export | Implemented and tested in `infra/db.yaml` (`tests/test_infra.py`, cfn-lint). **Deployed 2026-11-13** with the defaults (7 days, protection on; `KB_DB_BACKUP_DAYS` unset): `kb-db` created, and on delete CloudFormation took the final snapshot `kb-db-snapshot-db-uz70pqrr8z1u` (20 GB) |
| `infra/teardown-db.sh`, dated laptop copies in `infra/up.sh save` | Implemented and tested with stub AWS commands (phase 26, `tests/test_infra.py`). **Run against AWS 2026-11-13:** `save` kept the 65-document copy as `saved/kb-20261113T175011Z-before.db.gz`, checked the download (integrity ok) and replaced the laptop copy (79 docs, 1089 chunks, dated copy `saved/kb-20261113T175011Z.db.gz`); `teardown-db.sh` turned protection off and deleted `kb-db` |
| Lab permissions for modify, snapshot, restore and delete | Allowed by the IAM policy simulator (`infra/probe-rds.sh` section 8, 2026-11-08) for gp2 at 20 GB. Real calls 2026-11-13: modify (protection off) and delete with a final snapshot worked; restore not tried |
| 7 day retention accepted by the lab | Accepted: the 2026-11-13 `kb-db` was created with the default 7 days |
| RDS point-in-time restore | **Not tested against AWS** (left out of the 2026-11-13 live test; out of scope by user decision). `infra/restore-test-db.sh` is written and stub-tested; its check (`infra/scripts/verify_restore.py`) is tested against a local PostgreSQL (phase 26, `tests/test_restore_check.py`); the restore itself needs a deployed `kb-db` and approval |
| Switch back from PostgreSQL to SQLite (export restored by the SQLite boot path) | Tested locally end to end, vectors byte-identical, semantic search without a backfill (phase 26, `tests/test_portable.py`); not run against AWS (no `sqlite` mode deployment since phase 25) |
| Restore from an export (`import-sqlite` at boot) | Exercised on every rebuild in phases 20 to 24 (account `983926826929`): 784 chunks back each time; again 2026-11-13 (790 chunks) |
| RPO and RTO numbers above | Estimates from AWS documentation and earlier deploy times, except the rebuild time (measured 26 minutes in phase 24 and 27 min 20 s on 2026-11-13) |
