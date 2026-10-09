#!/usr/bin/env bash
# Bring kb-mcp up in the current AWS Academy sandbox account, from nothing or from
# any partial state, in one command. A timed-out sandbox can come back as a brand
# new, empty account, so every step checks what already exists and skips it.
#
#   infra/up.sh          deploy everything that is missing, then the app stack
#   infra/up.sh save     download the latest database snapshot to the laptop
#                        (run before the lab session ends; up.sh uploads it into
#                        the next account so the knowledge base survives a reset)
#
# Steps of infra/up.sh:
#   1. Check the AWS credentials and the instance profile (EMR_EC2_DefaultRole).
#   2. Deploy kb-network and kb-data (infra/deploy.sh; no-ops when unchanged).
#   3. Write tokens: if s3://<bucket>/config/write-tokens is missing, upload the
#      local tokens file, or generate new tokens into it first. Never printed.
#   4. Database: if the bucket has no snapshot and a saved one exists locally,
#      upload it, so the app restores it at boot.
#   5. Image: build and upload it (infra/push-image.sh --build) unless the bucket
#      already has the image for the current commit. Uncommitted changes always
#      rebuild.
#   6. Look up the CloudFront prefix list, delete a kb-app stack left in
#      ROLLBACK_COMPLETE, and deploy kb-app (infra/deploy.sh app <tag>).
#
# Local files, all outside the repo (KB_CONFIG_DIR, default ~/.config/kb-mcp):
#   up.env           settings, read as shell variables:
#                      KB_ALERT_EMAIL=you@example.com          (required)
#                      KB_INSTANCE_PROFILE=EMR_EC2_DefaultRole (optional)
#                      KB_METRICS_MODE, KB_EXTENSION_ORIGIN     (optional, see deploy.sh)
#   write-tokens     name:secret,name:secret (created on the first run if missing)
#   kb-latest.db.gz  the snapshot saved by "infra/up.sh save"
set -euo pipefail

REGION="us-east-1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${KB_CONFIG_DIR:-$HOME/.config/kb-mcp}"
CONFIG_FILE="$CONFIG_DIR/up.env"
TOKENS_FILE="$CONFIG_DIR/write-tokens"
SNAPSHOT_FILE="$CONFIG_DIR/kb-latest.db.gz"
TOKENS_KEY="config/write-tokens"
SNAPSHOT_KEY="snapshots/kb-latest.db.gz"

step() { echo "==> $*" >&2; }
die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  echo "usage: infra/up.sh [save]" >&2
  exit 2
}

# object_exists KEY: true when s3://$BUCKET/KEY exists.
object_exists() {
  aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$1" > /dev/null 2>&1
}

# stack_status NAME: the stack status, or NONE when the stack does not exist.
stack_status() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2> /dev/null || echo NONE
}

# ready_stack NAME: delete a stack that a failed create left behind, and stop on
# one that is still changing (two deploys at once would collide).
ready_stack() {
  local name=$1 status
  status="$(stack_status "$name")"
  case "$status" in
    ROLLBACK_COMPLETE)
      step "$name is $status from a failed create; deleting it first"
      aws cloudformation delete-stack --region "$REGION" --stack-name "$name"
      aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$name"
      ;;
    *_IN_PROGRESS)
      die "$name is $status; wait until it finishes, then run infra/up.sh again"
      ;;
  esac
}

find_bucket() {
  BUCKET="$(aws cloudformation list-exports --region "$REGION" \
    --query "Exports[?Name=='kb-BucketName'].Value" --output text)"
  if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
    die "export kb-BucketName not found; is the kb-data stack deployed in this account?"
  fi
}

check_credentials() {
  local account
  if ! account="$(aws sts get-caller-identity --query Account --output text 2> /dev/null)"; then
    die "AWS credentials are missing or expired: Start Lab, paste the AWS CLI block into ~/.aws/credentials"
  fi
  step "AWS account $account"
}

save_snapshot() {
  check_credentials
  find_bucket
  object_exists "$SNAPSHOT_KEY" || die "s3://$BUCKET/$SNAPSHOT_KEY does not exist; nothing to save"
  mkdir -p "$CONFIG_DIR"
  aws s3 cp "s3://$BUCKET/$SNAPSHOT_KEY" "$SNAPSHOT_FILE.tmp" --region "$REGION" > /dev/null
  mv "$SNAPSHOT_FILE.tmp" "$SNAPSHOT_FILE"
  step "saved $SNAPSHOT_FILE; the next infra/up.sh uploads it if the bucket has no snapshot"
}

if [ $# -gt 1 ]; then
  usage
elif [ $# -eq 1 ]; then
  [ "$1" = "save" ] || usage
  save_snapshot
  exit 0
fi

# --- settings -------------------------------------------------------------------
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$CONFIG_FILE"
  set +a
fi
if [ -z "${KB_ALERT_EMAIL:-}" ]; then
  echo "error: KB_ALERT_EMAIL is not set. Create $CONFIG_FILE with a line like:" >&2
  echo "  KB_ALERT_EMAIL=you@example.com" >&2
  exit 2
fi
export KB_INSTANCE_PROFILE="${KB_INSTANCE_PROFILE:-EMR_EC2_DefaultRole}"

# --- 1. credentials and instance profile -------------------------------------------
check_credentials
if ! aws iam get-instance-profile --instance-profile-name "$KB_INSTANCE_PROFILE" > /dev/null 2>&1; then
  echo "error: instance profile $KB_INSTANCE_PROFILE not found in this account. Available:" >&2
  aws iam list-instance-profiles --query 'InstanceProfiles[].InstanceProfileName' --output text >&2 || true
  echo "Set KB_INSTANCE_PROFILE in $CONFIG_FILE to one of them." >&2
  exit 1
fi
step "instance profile $KB_INSTANCE_PROFILE"

# --- 2. network and data ------------------------------------------------------------
for stack in network data; do
  ready_stack "kb-$stack"
  "$ROOT/infra/deploy.sh" "$stack"
done
find_bucket
step "bucket $BUCKET"

# --- 3. write tokens --------------------------------------------------------------
if object_exists "$TOKENS_KEY"; then
  step "write tokens already in s3://$BUCKET/$TOKENS_KEY"
else
  if [ ! -s "$TOKENS_FILE" ]; then
    mkdir -p "$CONFIG_DIR"
    (
      umask 077
      python3 -c 'import secrets; print("temp:" + secrets.token_urlsafe(32) + ",demo:" + secrets.token_urlsafe(32))' \
        > "$TOKENS_FILE"
    )
    step "generated new write tokens in $TOKENS_FILE (not printed; open the file to copy them)"
  fi
  aws s3 cp "$TOKENS_FILE" "s3://$BUCKET/$TOKENS_KEY" --sse AES256 --region "$REGION" > /dev/null
  step "uploaded write tokens from $TOKENS_FILE"
fi

# --- 4. database snapshot ------------------------------------------------------------
if object_exists "$SNAPSHOT_KEY"; then
  step "database snapshot already in the bucket"
elif [ -s "$SNAPSHOT_FILE" ]; then
  aws s3 cp "$SNAPSHOT_FILE" "s3://$BUCKET/$SNAPSHOT_KEY" --sse AES256 --region "$REGION" > /dev/null
  step "uploaded the saved database snapshot $SNAPSHOT_FILE"
else
  step "no database snapshot in the bucket or saved locally; the app starts empty"
fi

# --- 5. image ------------------------------------------------------------------------
cd "$ROOT"
TAG="$(git rev-parse --short HEAD)"
if [ -z "$(git status --porcelain)" ] && object_exists "images/kb-mcp-$TAG.tar.gz"; then
  step "image $TAG already in the bucket"
else
  TAG="$("$ROOT/infra/push-image.sh" --build)"
fi
step "image tag $TAG"

# --- 6. app stack ---------------------------------------------------------------------
if [ -z "${KB_CLOUDFRONT_PREFIX_LIST_ID:-}" ]; then
  KB_CLOUDFRONT_PREFIX_LIST_ID="$(aws ec2 describe-managed-prefix-lists --region "$REGION" \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
    --query 'PrefixLists[0].PrefixListId' --output text)"
fi
case "$KB_CLOUDFRONT_PREFIX_LIST_ID" in
  pl-*) ;;
  *) die "could not find the CloudFront origin-facing prefix list (got '$KB_CLOUDFRONT_PREFIX_LIST_ID')" ;;
esac
export KB_CLOUDFRONT_PREFIX_LIST_ID KB_ALERT_EMAIL
step "CloudFront prefix list $KB_CLOUDFRONT_PREFIX_LIST_ID"

ready_stack kb-app
"$ROOT/infra/deploy.sh" app "$TAG"
step "done. Before the lab session ends, run: infra/up.sh save"
