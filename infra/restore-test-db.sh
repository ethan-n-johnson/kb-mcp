#!/usr/bin/env bash
# Prove that the RDS backups restore: a point-in-time copy of kb-db is created as a
# separate, temporary instance, checked from the laptop, and deleted again.
# kb-db itself is only read (its identifier and latest restorable time); it is
# never modified, stopped or deleted. docs/RECOVERY.md, "Restore test".
#
#   infra/restore-test-db.sh            create, check, delete (asks first)
#   infra/restore-test-db.sh --keep     leave the copy running (delete it later
#                                       with: infra/restore-test-db.sh --cleanup)
#   infra/restore-test-db.sh --cleanup  delete leftovers of an earlier run
#
# Creates (all named kb-restore-test*, tagged Purpose=restore-test):
#   - a security group in the kb VPC allowing 5432 from this laptop's address only
#   - a DB subnet group over the two public subnets (so the laptop can reach the copy)
#   - a db.t3.micro Single-AZ, publicly accessible, encrypted like the source,
#     restored to the latest restorable time
# Cost: about 0.017 USD per hour for the instance plus a few cents of gp2 storage;
# a full run takes about 20 to 40 minutes. The copy's master password is kb-db's
# (read from ~/.config/kb-mcp/db-password, never printed).
set -euo pipefail

REGION="us-east-1"
PREFIX="kb-restore-test"
SG_NAME="$PREFIX-sg"
SUBNET_GROUP="$PREFIX-subnets"
CONFIG_DIR="${KB_CONFIG_DIR:-$HOME/.config/kb-mcp}"
PASSWORD_FILE="$CONFIG_DIR/db-password"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${KB_PYTHON:-$ROOT/.venv/bin/python}"
MODE="run"

case "${1:-}" in
  "") ;;
  --keep) MODE="keep" ;;
  --cleanup) MODE="cleanup" ;;
  *)
    echo "usage: infra/restore-test-db.sh [--keep | --cleanup]" >&2
    exit 2
    ;;
esac

die() {
  echo "error: $*" >&2
  exit 1
}
step() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
aws_r() { aws --region "$REGION" "$@"; }
get_export() {
  aws_r cloudformation list-exports --query "Exports[?Name=='$1'].Value | [0]" --output text
}

# Deletes only what this script creates; refuses any other identifier.
delete_copy() {
  local id=$1
  [[ "$id" == "$PREFIX"-* ]] || die "refusing to delete $id: not a $PREFIX instance"
  step "deleting $id (no final snapshot)"
  aws_r rds delete-db-instance --db-instance-identifier "$id" \
    --skip-final-snapshot --delete-automated-backups > /dev/null || true
  aws_r rds wait db-instance-deleted --db-instance-identifier "$id" || true
}

cleanup() {
  local ids sg
  ids="$(aws_r rds describe-db-instances \
    --query "DBInstances[?starts_with(DBInstanceIdentifier, '$PREFIX-')].DBInstanceIdentifier" --output text)"
  for id in $ids; do delete_copy "$id"; done
  if aws_r rds describe-db-subnet-groups --db-subnet-group-name "$SUBNET_GROUP" > /dev/null 2>&1; then
    step "deleting DB subnet group $SUBNET_GROUP"
    aws_r rds delete-db-subnet-group --db-subnet-group-name "$SUBNET_GROUP" || true
  fi
  sg="$(aws_r ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2> /dev/null || true)"
  if [[ -n "$sg" && "$sg" != "None" ]]; then
    step "deleting security group $sg"
    aws_r ec2 delete-security-group --group-id "$sg" || true
  fi
  step "cleanup done"
}

aws_r sts get-caller-identity > /dev/null || die "AWS credentials do not work; refresh them from the AWS Academy console"

if [[ "$MODE" == "cleanup" ]]; then
  cleanup
  exit 0
fi

SOURCE_ID="$(get_export kb-DbInstanceId)"
[[ -n "$SOURCE_ID" && "$SOURCE_ID" != "None" ]] || die "kb-db is not deployed (no kb-DbInstanceId export)"
[[ "$SOURCE_ID" != "$PREFIX"-* ]] || die "the source looks like a test copy: $SOURCE_ID"
VPC_ID="$(get_export kb-VpcId)"
SUBNET_A="$(get_export kb-PublicSubnetA)"
SUBNET_B="$(get_export kb-PublicSubnetB)"
[[ -f "$PASSWORD_FILE" ]] || die "no $PASSWORD_FILE; the copy has kb-db's password and the check needs it"
[[ -x "$PYTHON" ]] || die "no Python with psycopg at $PYTHON (set KB_PYTHON)"
LATEST="$(aws_r rds describe-db-instances --db-instance-identifier "$SOURCE_ID" \
  --query 'DBInstances[0].LatestRestorableTime' --output text)"
MY_IP="$(curl -s -m 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "cannot find this laptop's public address"
TARGET_ID="$PREFIX-$(date -u +%Y%m%d%H%M%S)"

echo "Restore test plan:"
echo "  source (read only):    $SOURCE_ID, latest restorable time $LATEST"
echo "  creates:               $TARGET_ID (db.t3.micro, Single-AZ, public, 5432 from $MY_IP/32 only),"
echo "                         security group $SG_NAME, DB subnet group $SUBNET_GROUP"
echo "  cost and time:         about 0.017 USD per hour, 20 to 40 minutes"
if [[ "$MODE" == "keep" ]]; then
  echo "  afterwards:            kept running (--keep); delete with infra/restore-test-db.sh --cleanup"
else
  echo "  afterwards:            the copy, the subnet group and the security group are deleted"
fi
printf 'Type restore-test to start: '
read -r answer
[[ "$answer" == "restore-test" ]] || {
  echo "Aborted."
  exit 1
}

[[ "$MODE" == "keep" ]] || trap cleanup EXIT

START=$(date +%s)
step "creating security group $SG_NAME"
SG_ID="$(aws_r ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC_ID" \
  --description "kb-mcp restore test, 5432 from one laptop, temporary" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=Project,Value=kb-mcp},{Key=Purpose,Value=restore-test}]" \
  --query GroupId --output text)"
aws_r ec2 authorize-security-group-ingress --group-id "$SG_ID" \
  --protocol tcp --port 5432 --cidr "$MY_IP/32" > /dev/null
step "creating DB subnet group $SUBNET_GROUP"
aws_r rds create-db-subnet-group --db-subnet-group-name "$SUBNET_GROUP" \
  --db-subnet-group-description "kb-mcp restore test, public subnets, temporary" \
  --subnet-ids "$SUBNET_A" "$SUBNET_B" > /dev/null

step "restoring $SOURCE_ID to $TARGET_ID at its latest restorable time"
aws_r rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$SOURCE_ID" \
  --target-db-instance-identifier "$TARGET_ID" \
  --use-latest-restorable-time \
  --db-instance-class db.t3.micro --no-multi-az --publicly-accessible \
  --db-subnet-group-name "$SUBNET_GROUP" --vpc-security-group-ids "$SG_ID" \
  --storage-type gp2 --no-deletion-protection --backup-retention-period 0 \
  --tags Key=Project,Value=kb-mcp Key=Purpose,Value=restore-test > /dev/null
aws_r rds wait db-instance-available --db-instance-identifier "$TARGET_ID"
READY=$(date +%s)
step "$TARGET_ID is available after $(((READY - START) / 60)) min $(((READY - START) % 60)) s"
HOST="$(aws_r rds describe-db-instances --db-instance-identifier "$TARGET_ID" \
  --query 'DBInstances[0].Endpoint.Address' --output text)"

step "checking the copy from the laptop"
KB_RESTORE_HOST="$HOST" KB_RESTORE_PASSWORD_FILE="$PASSWORD_FILE" "$PYTHON" -I - << 'PY'
import os
import psycopg

password = open(os.environ["KB_RESTORE_PASSWORD_FILE"], encoding="utf-8").read().strip()
conn = psycopg.connect(
    host=os.environ["KB_RESTORE_HOST"], port=5432, dbname="kb", user="kbadmin",
    password=password, sslmode="require", connect_timeout=15,
)
q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
docs, chunks = q("SELECT count(*) FROM docs"), q("SELECT count(*) FROM chunks")
embedded = q("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
vector = q("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
latest = q("SELECT max(updated_at) FROM docs")
ok = docs > 0 and chunks > 0 and embedded == chunks and vector is not None
if embedded:
    # A real cosine query over the restored vectors, as search runs it.
    q("SELECT c.id FROM chunks c, (SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1) s "
      "ORDER BY c.embedding <=> s.embedding LIMIT 5")
conn.close()
print(f"restored copy: {docs} docs, {chunks} chunks, {embedded} embedded, pgvector {vector}, newest doc {latest}")
print("RESTORE CHECK PASSED" if ok else "RESTORE CHECK FAILED")
raise SystemExit(0 if ok else 1)
PY
DONE=$(date +%s)
step "restore and check took $(((DONE - START) / 60)) min $(((DONE - START) % 60)) s (record this as the measured RTO part)"
