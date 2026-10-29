# Phase 19: RDS probe and kb-db stack

**Days:** Thu Oct 29 to Fri Oct 30 · **Depends on:** 12 · **Where:** AWS, then local · **PLAN.md sections:** Course alignment (Database layer); Sandbox changes

## Goal

Find out what RDS the sandbox allows, then add a fourth CloudFormation stack, `kb-db`, with an RDS PostgreSQL instance that has pgvector, reachable only from the app instances. Nothing in the app changes yet.

## Read first

- `CLAUDE.md` ("AWS Academy sandbox rules"), `docs/PROGRESS.md` (Environment, deviations 6 to 8)
- `infra/network.yaml` (exports), `infra/data.yaml`, `infra/deploy.sh`, `infra/up.sh`
- `infra/probe-rds.sh`

## Tasks

1. **HUMAN:** start a lab session, refresh `~/.aws/credentials`, run `infra/up.sh` (so `kb-network` exists), then `infra/probe-rds.sh --try-subnet-group`. Paste the output into `docs/PROGRESS.md` under Environment as rows: PostgreSQL version chosen, instance classes offered, Multi-AZ simulation result, subnet group create. Also record the pgvector check done on Oct 28: the probe's `rds.extensions` list omits `vector`, but a throwaway public `db.t3.micro` on 16.9 (deleted afterwards) answered `pg_available_extensions` with `vector 0.8.0`, `CREATE EXTENSION vector` succeeded, and `'[1,2,3]'::vector <=> '[1,2,4]'::vector` returned 0.0085. Probe results on Oct 28: 16.9 to 18.6 offered (default 18.3), `db.t3.micro` to `db.t3.medium` and `db.t4g.micro`/`small` all Multi-AZ capable on gp2, IAM simulation allows `CreateDBInstance` with and without Multi-AZ, subnet group create and delete OK, 0 of 40 DB instances used.
2. Record under Deviations as "Phase 2 database settings": PostgreSQL `16.9`, `db.t3.micro`, `MultiAz=true` (fall back to `false` only if the create is refused, or if the user says sandbox credit is tight), 20 GB `gp2`. 16.9 matches the local `pgvector/pgvector:pg16` image used in tests.
3. `infra/db.yaml`:
   - Parameters: `DbInstanceClass`, `EngineVersion`, `MultiAz` (`true` or `false`), `DbPassword` (`NoEcho`, 16 to 64 letters and digits), `StorageEncrypted` (default `true`).
   - `DbSubnetGroup` over `kb-PrivateSubnetA` and `kb-PrivateSubnetB`.
   - `DbClientSg`: an empty security group, exported as `kb-DbClientSg`. The app stack attaches it to the instances in phase 21.
   - `DbSg`: ingress TCP 5432 from `DbClientSg` only, no other ingress.
   - `Db` (`AWS::RDS::DBInstance`): engine `postgres`, database name `kb`, master user `kbadmin`, 20 GB `gp2`, `PubliclyAccessible: false`, `MultiAZ: !Ref MultiAz`, `BackupRetentionPeriod: 1`, `DeletionProtection: false`, `DeletionPolicy: Delete` and `UpdateReplacePolicy: Delete` (a sandbox reset loses RDS anyway; the S3 export in phase 20 is the backup that survives).
   - Outputs and exports: `kb-DbEndpoint`, `kb-DbPort`, `kb-DbClientSg`.
4. `infra/deploy.sh db`: same pattern as the other stacks (cfn-lint, `validate-template`, `deploy --no-fail-on-empty-changeset`). The password comes from the environment variable `KB_DB_PASSWORD` and is never echoed.
5. `infra/up.sh`, new step between `kb-data` and the image:
   - Generate `~/.config/kb-mcp/db-password` (mode 600, 32 random letters and digits) if missing. Never print it.
   - Deploy `kb-db` with it (allow 10 to 25 minutes; Multi-AZ takes longer).
   - Write `postgresql://kbadmin:<password>@<endpoint>:5432/kb?sslmode=require` to `s3://<bucket>/config/database-url` with `--sse AES256`, exactly as the write tokens are stored. The instance reads it through `myS3Role` in phase 20.
6. **Fallbacks, decided by what AWS says** (record each one used as a deviation):
   - Multi-AZ create fails with AccessDenied or `InvalidParameterCombination`: redeploy with `MultiAz=false`. The course point (a standby in the other AZ) moves to the slides as the production setting.
   - `StorageEncrypted: true` fails (KMS is list only): `StorageEncrypted=false`, recorded in `docs/SECURITY_REVIEW.md`.
   - The instance class is denied: try the next one in task 2's list.
7. Check the instance with `aws rds describe-db-instances`: status `available`, the endpoint, `MultiAZ` and `StorageEncrypted`. The laptop cannot reach a private database and there is no SSM, so the first real connection (and `CREATE EXTENSION vector`) happens at boot in phase 20.
8. Tests (`tests/test_infra.py` style): `db.yaml` passes cfn-lint; `DbSg` has exactly one ingress rule and it references `DbClientSg`; `PubliclyAccessible` is false; `DbPassword` is `NoEcho`; `up.sh` never echoes the password (grep the script for the variable in an `echo` or `step` line); `probe-rds.sh` and the new `up.sh` pass shellcheck and are executable.
9. `docs/SETUP.md`: the probe command, the new `db-password` file, and "kb-db takes 10 to 25 minutes on the first deploy". `CLAUDE.md`: stacks are now `kb-network`, `kb-data`, `kb-db`, `kb-app`; `kb-db` may be deleted between sessions only after an export (phase 20).

## Do not

- Make the database public or open 5432 to a CIDR.
- Put the password in a template default, the repo, user data, or a log line.
- Touch the app code. Phase 20 does that.

## Acceptance

- [ ] Probe output recorded in PROGRESS.md; chosen settings and any fallback recorded as deviations.
- [ ] `aws rds describe-db-instances --db-instance-identifier <id>` shows `available`, `PubliclyAccessible: false`, and the recorded `MultiAZ` and `StorageEncrypted` values.
- [ ] `s3://<bucket>/config/database-url` exists (`head-object`), and nothing printed its contents.
- [ ] `infra/up.sh` run twice in a row: the second run reports no changes for `kb-db`.
- [ ] cfn-lint clean; `pytest -q` passes.
- [ ] Phase log updated; commit `phase 19: kb-db stack with RDS PostgreSQL`.
