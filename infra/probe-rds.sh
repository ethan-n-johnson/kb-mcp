#!/usr/bin/env bash
# Probe what the AWS Academy sandbox allows for RDS PostgreSQL before phase 19
# builds the kb-db stack. Read only by default: nothing is created and nothing bills.
#
#   infra/probe-rds.sh                       read-only checks
#   infra/probe-rds.sh --try-subnet-group    also create and delete a DB subnet group
#                                            in the kb-network private subnets (free)
#
# Prints one line per check and a summary. Paste the whole output into
# docs/PROGRESS.md ("Environment") when it is done.
#
# What it checks:
#   1. Credentials (sts get-caller-identity).
#   2. PostgreSQL versions RDS offers here, newest first, and the default one.
#   3. The rds.extensions parameter for that major version (informational only:
#      it omits "vector" even though RDS PostgreSQL 16 supports pgvector).
#   4. Small instance classes offered for that version, and which are Multi-AZ capable.
#   5. RDS account quotas (DB instances, storage).
#   6. Best effort: an IAM policy simulation of rds:CreateDBInstance for
#      db.t3.micro single-AZ and Multi-AZ. The sandbox may also deny through rules a
#      simulation cannot see, so a real create in phase 19 is the final answer.
#   7. With --try-subnet-group: create and delete a DB subnet group (proves
#      rds:CreateDBSubnetGroup and that the two private subnets are in two AZs).
#   8. Backups (phase 25.5): simulation of modify, snapshot, restore and delete
#      actions, and for a deployed kb-db its retention, deletion protection,
#      latest restorable time and snapshots. Read only.
set -euo pipefail

REGION="us-east-1"
CLASSES="db.t3.micro db.t3.small db.t4g.micro db.t4g.small db.t3.medium"
TRY_SUBNET_GROUP=0

case "${1:-}" in
  "") ;;
  --try-subnet-group) TRY_SUBNET_GROUP=1 ;;
  *)
    echo "usage: infra/probe-rds.sh [--try-subnet-group]" >&2
    exit 2
    ;;
esac

ok() { printf 'OK    %s\n' "$*"; }
bad() { printf 'NO    %s\n' "$*"; }
info() { printf '      %s\n' "$*"; }

aws_q() { aws --region "$REGION" "$@" 2> /dev/null; }

echo "== 1. credentials"
if ! CALLER="$(aws_q sts get-caller-identity --query Arn --output text)"; then
  bad "AWS credentials do not work (paste fresh ones from AWS Details, then rerun)"
  exit 1
fi
ok "caller $CALLER"

echo "== 2. PostgreSQL versions"
VERSIONS="$(aws_q rds describe-db-engine-versions --engine postgres \
  --query 'reverse(sort_by(DBEngineVersions,&EngineVersion))[].EngineVersion' --output text || true)"
if [[ -z "$VERSIONS" ]]; then
  bad "rds:DescribeDBEngineVersions denied or no versions returned"
  exit 1
fi
DEFAULT_VERSION="$(aws_q rds describe-db-engine-versions --engine postgres --default-only \
  --query 'DBEngineVersions[0].EngineVersion' --output text || true)"
# Prefer the newest 16.x (pgvector 0.7+ and a long support window); else the default.
VERSION="$(tr '\t' '\n' <<< "$VERSIONS" | grep -E '^16\.' | head -1 || true)"
VERSION="${VERSION:-$DEFAULT_VERSION}"
MAJOR="${VERSION%%.*}"
ok "newest offered: $(tr '\t' '\n' <<< "$VERSIONS" | head -3 | paste -sd' ')"
ok "default: ${DEFAULT_VERSION:-unknown}; probing with $VERSION (major $MAJOR)"

echo "== 3. pgvector"
# The rds.extensions default parameter does not list "vector", yet RDS ships it:
# a throwaway db.t3.micro on 16.9 answered CREATE EXTENSION vector with 0.8.0
# (phase 19 probe). So this only reports the parameter, it does not decide.
EXTENSIONS="$(aws_q rds describe-engine-default-parameters --db-parameter-group-family "postgres$MAJOR" \
  --query "EngineDefaults.Parameters[?ParameterName=='rds.extensions'].AllowedValues | [0]" --output text || true)"
if tr ',' '\n' <<< "${EXTENSIONS:-}" | tr -d ' ' | grep -qx vector; then
  ok "vector is listed in rds.extensions for postgres$MAJOR"
else
  info "vector is not in the rds.extensions list for postgres$MAJOR; that list is incomplete."
  info "Confirm with SELECT * FROM pg_available_extensions WHERE name='vector' after the create."
fi

echo "== 4. instance classes for postgres $VERSION"
for cls in $CLASSES; do
  ROW="$(aws_q rds describe-orderable-db-instance-options --engine postgres --engine-version "$VERSION" \
    --db-instance-class "$cls" --query 'OrderableDBInstanceOptions[0].[MultiAZCapable,StorageType]' \
    --output text || true)"
  if [[ -z "$ROW" || "$ROW" == "None" ]]; then
    bad "$cls not offered"
  else
    read -r MULTI STORAGE <<< "$ROW"
    ok "$cls offered (Multi-AZ capable: $MULTI, storage: $STORAGE)"
  fi
done

echo "== 5. quotas"
# shellcheck disable=SC2016  # backticks are JMESPath literals, not shell
if QUOTAS="$(aws_q rds describe-account-attributes \
  --query 'AccountQuotas[?AccountQuotaName==`DBInstances` || AccountQuotaName==`AllocatedStorage` || AccountQuotaName==`DBSubnetGroups`].[AccountQuotaName,Used,Max]' \
  --output text)"; then
  while read -r name used max; do info "$name used $used of $max"; done <<< "$QUOTAS"
else
  bad "rds:DescribeAccountAttributes denied"
fi

echo "== 6. IAM simulation (best effort)"
# An assumed-role ARN (arn:aws:sts::ACCT:assumed-role/NAME/SESSION) maps to the role ARN.
ROLE_ARN=""
if [[ "$CALLER" =~ ^arn:aws:sts::([0-9]+):assumed-role/([^/]+)/ ]]; then
  ROLE_ARN="arn:aws:iam::${BASH_REMATCH[1]}:role/${BASH_REMATCH[2]}"
fi
if [[ -z "$ROLE_ARN" ]]; then
  info "caller is not an assumed role; skipped"
else
  for multi in false true; do
    DECISION="$(aws_q iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
      --action-names rds:CreateDBInstance \
      --context-entries "ContextKeyName=aws:RequestedRegion,ContextKeyValues=$REGION,ContextKeyType=string" \
      "ContextKeyName=rds:DatabaseClass,ContextKeyValues=db.t3.micro,ContextKeyType=string" \
      "ContextKeyName=rds:MultiAz,ContextKeyValues=$multi,ContextKeyType=boolean" \
      "ContextKeyName=rds:DatabaseEngine,ContextKeyValues=postgres,ContextKeyType=string" \
      --query 'EvaluationResults[0].EvalDecision' --output text || true)"
    if [[ -z "$DECISION" ]]; then
      info "simulation not allowed for this role; skipped"
      break
    fi
    if [[ "$DECISION" == "allowed" ]]; then ok "CreateDBInstance db.t3.micro MultiAz=$multi: allowed"; else bad "CreateDBInstance db.t3.micro MultiAz=$multi: $DECISION"; fi
  done
fi

if [[ "$TRY_SUBNET_GROUP" == 1 ]]; then
  echo "== 7. DB subnet group (create, then delete)"
  get_export() {
    aws_q cloudformation list-exports --query "Exports[?Name=='$1'].Value | [0]" --output text
  }
  SUBNET_A="$(get_export kb-PrivateSubnetA || true)"
  SUBNET_B="$(get_export kb-PrivateSubnetB || true)"
  if [[ -z "$SUBNET_A" || "$SUBNET_A" == "None" || -z "$SUBNET_B" || "$SUBNET_B" == "None" ]]; then
    bad "kb-network exports not found; run infra/up.sh first"
  else
    NAME="kb-probe-$(date +%s)"
    if aws_q rds create-db-subnet-group --db-subnet-group-name "$NAME" \
      --db-subnet-group-description "kb-mcp probe, safe to delete" \
      --subnet-ids "$SUBNET_A" "$SUBNET_B" > /dev/null; then
      ok "created DB subnet group $NAME"
      if aws_q rds delete-db-subnet-group --db-subnet-group-name "$NAME"; then
        ok "deleted $NAME"
      else
        bad "could not delete $NAME; delete it in the RDS console (Subnet groups)"
      fi
    else
      bad "rds:CreateDBSubnetGroup denied, or the subnets are not in two AZs"
    fi
  fi
fi

echo "== 8. Backups, restore and deletion protection (phase 25.5, read only)"
# The context must carry what a real gp2 request carries: aws:RequestedRegion,
# rds:Piops=0 and rds:StorageSize. The lab policy denies provisioned IOPS and
# large storage, and the simulator treats a missing key as a match for those
# denies, so leaving one out reports a false explicit deny (seen 2026-11-08: with
# Piops=0 and 20 GB, modify and both restores are allowed; 200 GB or 1000 IOPS
# are denied).
if [[ -z "$ROLE_ARN" ]]; then
  info "caller is not an assumed role; simulation skipped"
else
  for action in rds:ModifyDBInstance rds:CreateDBSnapshot rds:RestoreDBInstanceToPointInTime \
    rds:RestoreDBInstanceFromDBSnapshot rds:DeleteDBInstance rds:DeleteDBSnapshot; do
    DECISION="$(aws_q iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
      --action-names "$action" \
      --context-entries "ContextKeyName=aws:RequestedRegion,ContextKeyValues=$REGION,ContextKeyType=string" \
      "ContextKeyName=rds:DatabaseClass,ContextKeyValues=db.t3.micro,ContextKeyType=string" \
      "ContextKeyName=rds:DatabaseEngine,ContextKeyValues=postgres,ContextKeyType=string" \
      "ContextKeyName=rds:Piops,ContextKeyValues=0,ContextKeyType=numeric" \
      "ContextKeyName=rds:StorageSize,ContextKeyValues=20,ContextKeyType=numeric" \
      --query 'EvaluationResults[0].EvalDecision' --output text || true)"
    if [[ "$DECISION" == "allowed" ]]; then ok "$action: allowed"; else bad "$action: ${DECISION:-simulation not allowed}"; fi
  done
fi
DB_ID="$(aws_q cloudformation list-exports --query "Exports[?Name=='kb-DbInstanceId'].Value | [0]" --output text || true)"
if [[ -z "$DB_ID" || "$DB_ID" == "None" ]]; then
  info "kb-db is not deployed (no kb-DbInstanceId export); nothing to inspect"
else
  # shellcheck disable=SC2016  # backticks are JMESPath literals, not shell
  aws_q rds describe-db-instances --db-instance-identifier "$DB_ID" \
    --query 'DBInstances[0].[BackupRetentionPeriod,DeletionProtection,MultiAZ,PreferredBackupWindow,LatestRestorableTime]' \
    --output text | while read -r days protected multi window latest; do
      if [[ "$days" -ge 7 ]]; then ok "$DB_ID backup retention $days days"; else bad "$DB_ID backup retention $days days (want 7)"; fi
      if [[ "$protected" == "True" ]]; then ok "$DB_ID deletion protection on"; else bad "$DB_ID deletion protection off"; fi
      info "Multi-AZ $multi, backup window $window, latest restorable time $latest"
    done
  SNAPS="$(aws_q rds describe-db-snapshots --db-instance-identifier "$DB_ID" \
    --query 'DBSnapshots[].[SnapshotType,DBSnapshotIdentifier,SnapshotCreateTime]' --output text || true)"
  if [[ -n "$SNAPS" ]]; then while read -r kind name when; do info "snapshot $kind $name $when"; done <<< "$SNAPS"; else info "no snapshots yet"; fi
fi

echo "== done. Paste this output into docs/PROGRESS.md (Environment) and phase 19 picks the settings from it."
