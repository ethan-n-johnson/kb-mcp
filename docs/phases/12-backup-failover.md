# Phase 12: Backup, restore, failover

**Day:** 5 (Sun Oct 11) · **Depends on:** 11 · **Where:** AWS · **PLAN.md sections:** Course alignment (Disaster planning); AWS deployment; Demo script step 8; Risks (instance replacement row)

## Goal

Nightly SQLite backups to S3 through the gateway endpoint, continuous S3 snapshots of the live database (the app uploads one a few seconds after every write; there is no shared file system in this sandbox), a tested restore, and a measured failover where the Auto Scaling group replaces a terminated instance and the replacement restores the data from the snapshot.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md`
- `docs/PLAN.md`: "Sandbox changes" (the Disaster planning replacement row), demo step 8
- `kb/snapshot.py`, `kb/backup.py` (snapshot and restore source order; the snapshots code was written with the sandbox change)

## Tasks

1. `python -m kb.cli backup`: copy the live DB with `sqlite3.Connection.backup()` to a temp file, run `PRAGMA integrity_check` on the copy (must be `ok`), gzip it, upload to `s3://$KB_BUCKET/backups/kb-YYYYMMDD-HHMMSS.db.gz`, print the key. Exit non-zero on any failure.
2. `python -m kb.cli restore (--latest | --key K) [--target PATH]`: download, gunzip, integrity check, move the current DB aside as `kb.db.pre-restore-<ts>`, put the restored file in place. Refuse to run without `--force`, since replacing the file under a running app corrupts its view; the on-instance script below passes `--force` only after stopping the container. `--latest` means the snapshot `snapshots/kb-latest.db.gz` first, else the newest object under `backups/`, else nothing (exit 0, the boot script then starts an empty database). Restoring a backup over the live database also uploads it as the new snapshot, so a later replacement instance does not restore the state the restore undid.
3. `infra/scripts/restore-on-instance.sh` (run on the instance): `docker stop kb` (the shutdown uploads a last snapshot), run the restore in a one-off container with the same image and `/data` mount, `docker start kb`, then poll `/healthz`. After a bad delete, `--latest` restores the bad delete (the snapshot already has it): use `--key backups/...` or an earlier S3 version of the snapshot.
4. Backups in user data (replace the phase 06 placeholder): write `/etc/cron.d/kb-backup` with `15 7 * * * root docker exec kb python -m kb.cli backup >> /var/log/kb-backup.log 2>&1` (07:15 UTC is about 2:15 AM Central). `cronie` was installed in phase 06; confirm `crond` is enabled.
5. `infra/data.yaml`: lifecycle rule expiring objects under `backups/` after 30 days (versioning keeps the snapshot history; the noncurrent-version rule removes old snapshot versions after 30 days). Update `kb-data` (data is retained, update is safe).
6. Tests (moto): backup then restore round-trips a DB with docs; a corrupted backup fails the integrity check and leaves the current DB untouched; `restore --latest` takes the snapshot, else the newest backup, else exits 0 with nothing restored; every write route marks the database dirty, a burst of writes makes one upload, shutdown flushes, a failed upload is retried and never raised (`tests/test_snapshot.py`, `tests/test_commit_hook.py`).
7. Deploy the new image and user data. Run a backup now via `ssm send-command` and confirm the object exists.
7a. Snapshot check: upload one doc through `POST /api/docs`, then within about 15 seconds `aws s3api head-object --bucket <bucket> --key snapshots/kb-latest.db.gz` succeeds with a `LastModified` after the upload, and the container log (`aws logs filter-log-events --log-group-name /kb/app --filter-pattern '"snapshot uploaded"'`, once phase 15 is deployed, or `docker logs kb` via `ssm send-command`) has a `snapshot uploaded` line with `key`, `bytes` and `ms`. Run `aws s3api list-object-versions --bucket <bucket> --prefix snapshots/` and confirm several versions exist.
8. Restore drill on the instance: note the doc count, run `restore-on-instance.sh --key <a backup key>` (and once with `--latest`), confirm the doc count matches what the source held and search works.
9. Failover drill. **Ask the user before terminating.** Then:
   - Upload one doc, wait for its snapshot (step 7a), then note the doc count, the instance ID and its AZ. Start a timer.
   - `aws ec2 terminate-instances --instance-ids <id>`.
   - Poll target health and `/healthz` (through CloudFront if phase 10 is done) every 15 s.
   - Record: time to unhealthy, time to new instance launch, its AZ (ideally the other AZ; if it lands in the same one, say so), time to healthy. From the new instance's `/var/log/kb-bootstrap.log` and the container logs, record the image download and load time, the restore time (the `restored` log line shows `key` and `docs`), model load time and index build time.
   - Confirm the replacement restored from the snapshot (`restored` line with `key` `snapshots/kb-latest.db.gz`), and that the doc count and a demo search are unchanged, including the doc uploaded just before the termination.
   - Note that a terminate sends the OS shutdown, so the old instance usually flushes a last snapshot; to see the worst case once, say so to the user and only then do an abrupt stop (`aws ec2 stop-instances --force`), which loses at most the writes of the last `KB_SNAPSHOT_INTERVAL` seconds.
10. If total recovery is over about 6 minutes, write down which step dominates and one fix to consider (smaller image or a shorter download, shorter health check interval) under "Open issues". Do not implement it unless the user asks.

## Do not

- Switch the journal mode (WAL would break the one-file snapshot and restore).
- Restore onto a running app, or start the app empty when the restore could not read S3.

## Acceptance

- [ ] `aws s3 ls s3://<bucket>/backups/` shows at least one backup.
- [ ] `aws s3 ls s3://<bucket>/snapshots/` shows `kb-latest.db.gz`, newer than the last write minus about `KB_SNAPSHOT_INTERVAL` seconds.
- [ ] `/etc/cron.d/kb-backup` exists on the instance (check via `ssm send-command`).
- [ ] Restore drill passed with matching doc counts.
- [ ] Failover drill passed: the replacement restored from the snapshot (log line shows the snapshot key), doc count and search unchanged, AZ of the replacement recorded; timings recorded in PROGRESS.md Outputs ("Instance replacement time").
- [ ] Phase log updated; commit `phase 12: backup, restore, failover`.
