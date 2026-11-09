#!/usr/bin/env bash
# Delete the kb-db stack (RDS PostgreSQL) deliberately, without losing the data.
#
# Order for a session end in postgres mode (docs/RECOVERY.md, "Planned teardown"):
#   1. infra/up.sh save          the app exports to S3, the laptop downloads it
#   2. infra/teardown-app.sh     kb-app imports kb-db's exports, so it goes first
#   3. infra/teardown-db.sh      this script
#
# What this script does:
#   - refuses while kb-app exists (CloudFormation would refuse the delete too);
#   - refuses unless the laptop copy (~/.config/kb-mcp/kb-latest.db.gz) is younger
#     than KB_TEARDOWN_MAX_AGE_MIN minutes (default 60) and passes an integrity
#     check, because a sandbox reset destroys RDS snapshots and the bucket as well;
#   - asks you to type the stack name;
#   - turns deletion protection off on the instance (DeletionProtection=true in
#     db.yaml makes a stray delete-stack fail), then deletes the stack. Its
#     DeletionPolicy: Snapshot leaves a final RDS snapshot. It is kept until you
#     delete it (aws rds delete-db-snapshot) or the sandbox is reset.
#   - if the delete fails, turns deletion protection back on.
set -euo pipefail

REGION="us-east-1"
STACK="kb-db"
CONFIG_DIR="${KB_CONFIG_DIR:-$HOME/.config/kb-mcp}"
SNAPSHOT_FILE="$CONFIG_DIR/kb-latest.db.gz"
MAX_AGE_MIN="${KB_TEARDOWN_MAX_AGE_MIN:-60}"

die() {
  echo "error: $*" >&2
  exit 1
}

stack_exists() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" > /dev/null 2>&1
}

if ! stack_exists "$STACK"; then
  echo "Stack $STACK does not exist (or is not visible). Nothing to delete."
  exit 0
fi
if stack_exists kb-app; then
  die "kb-app still exists and imports kb-db's exports. Run infra/up.sh save, then infra/teardown-app.sh, then this script."
fi

# The laptop copy is the only backup that survives a sandbox reset.
[ -f "$SNAPSHOT_FILE" ] || die "no laptop copy at $SNAPSHOT_FILE; run infra/up.sh save while kb-app is still up"
if [ -z "$(find "$SNAPSHOT_FILE" -mmin "-$MAX_AGE_MIN" -print)" ]; then
  die "$SNAPSHOT_FILE is older than $MAX_AGE_MIN minutes; run infra/up.sh save first (set KB_TEARDOWN_MAX_AGE_MIN to change the limit)"
fi
python3 -I - "$SNAPSHOT_FILE" << 'PY' || die "$SNAPSHOT_FILE failed its check; do not delete the database"
import gzip, shutil, sqlite3, sys, tempfile
with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
    with gzip.open(sys.argv[1], "rb") as src:
        shutil.copyfileobj(src, tmp)
    tmp.flush()
    conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    docs = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    conn.close()
print(f"laptop copy: integrity {'ok' if ok else 'FAILED'}, {docs} docs, {chunks} chunks")
sys.exit(0 if ok else 1)
PY

DB_ID="$(aws cloudformation describe-stack-resource --region "$REGION" --stack-name "$STACK" \
  --logical-resource-id Db --query 'StackResourceDetail.PhysicalResourceId' --output text)"
[ -n "$DB_ID" ] && [ "$DB_ID" != "None" ] || die "cannot find the database instance of $STACK"
PROTECTED="$(aws rds describe-db-instances --region "$REGION" --db-instance-identifier "$DB_ID" \
  --query 'DBInstances[0].DeletionProtection' --output text)"

echo "This will delete the CloudFormation stack $STACK in $REGION:"
echo "  RDS instance $DB_ID (deletion protection: $PROTECTED), its subnet group and security groups."
echo "  CloudFormation takes a final snapshot first (DeletionPolicy: Snapshot)."
echo "  Automated backups are deleted with the instance; the final snapshot stays."
printf 'Type the stack name (%s) to confirm: ' "$STACK"
read -r answer
if [ "$answer" != "$STACK" ]; then
  echo "Aborted."
  exit 1
fi

set_protection() {
  aws rds modify-db-instance --region "$REGION" --db-instance-identifier "$DB_ID" \
    "$1" --apply-immediately > /dev/null
}

if [ "$PROTECTED" = "True" ]; then
  echo "Turning deletion protection off on $DB_ID..."
  set_protection --no-deletion-protection
  for _ in $(seq 1 30); do
    now="$(aws rds describe-db-instances --region "$REGION" --db-instance-identifier "$DB_ID" \
      --query 'DBInstances[0].DeletionProtection' --output text)"
    [ "$now" = "False" ] && break
    sleep 10
  done
  [ "$now" = "False" ] || die "deletion protection is still on after 5 minutes; nothing was deleted"
fi

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
echo "Waiting for $STACK to be deleted (final snapshot, then the instance; 10 to 20 minutes)..."
if ! aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"; then
  if [ "$PROTECTED" = "True" ]; then
    echo "The delete did not finish; turning deletion protection back on." >&2
    set_protection --deletion-protection || echo "could not turn deletion protection back on; do it in the console" >&2
  fi
  die "delete of $STACK failed; see: aws cloudformation describe-stack-events --stack-name $STACK"
fi
echo "Deleted $STACK. Final snapshots of $DB_ID:"
aws rds describe-db-snapshots --region "$REGION" --db-instance-identifier "$DB_ID" --snapshot-type manual \
  --query 'DBSnapshots[].[DBSnapshotIdentifier,SnapshotCreateTime,Status]' --output text || true
