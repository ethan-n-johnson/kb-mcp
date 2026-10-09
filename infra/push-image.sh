#!/usr/bin/env bash
# Tag the local kb-mcp image with the current git commit and upload it to S3.
#
#   infra/push-image.sh           upload the existing local image kb-mcp:dev
#   infra/push-image.sh --build   build it first (linux/amd64), then upload
#
# The sandbox has no ECR, so the instance fetches the image from the data
# bucket instead: docker save | gzip | s3://<bucket>/images/kb-mcp-<tag>.tar.gz.
# The user data in infra/app.yaml downloads that object and runs docker load.
#
# The tag is the short commit hash, with "-dirty" appended when the working tree
# has uncommitted changes. The tag is the only line written to stdout, so
#   TAG=$(infra/push-image.sh)
# works. Progress and docker output go to stderr.
set -euo pipefail

REGION="us-east-1"
LOCAL_IMAGE="kb-mcp:dev"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" = "--build" ]; then
  echo "==> docker build $LOCAL_IMAGE" >&2
  docker build --platform linux/amd64 -t "$LOCAL_IMAGE" . >&2
elif [ $# -gt 0 ]; then
  echo "usage: infra/push-image.sh [--build]" >&2
  exit 2
fi

if ! docker image inspect "$LOCAL_IMAGE" > /dev/null 2>&1; then
  echo "Local image $LOCAL_IMAGE not found. Build it or rerun with --build." >&2
  exit 1
fi

TAG="$(git rev-parse --short HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  TAG="$TAG-dirty"
  echo "warning: uncommitted changes, tagging as $TAG" >&2
fi

# The bucket name comes from the kb-data export, so nothing is hardcoded.
BUCKET="$(aws cloudformation list-exports \
  --region "$REGION" \
  --query "Exports[?Name=='kb-BucketName'].Value" \
  --output text)"
if [ -z "$BUCKET" ] || [ "$BUCKET" = "None" ]; then
  echo "Export kb-BucketName not found. Deploy the data stack first: infra/deploy.sh data" >&2
  exit 1
fi

KEY="images/kb-mcp-$TAG.tar.gz"
echo "==> docker tag $LOCAL_IMAGE kb-mcp:$TAG" >&2
docker tag "$LOCAL_IMAGE" "kb-mcp:$TAG"

# pipefail makes a failing docker save fail the pipeline, but aws would already
# have finished uploading the truncated stream by then, so remove that object.
echo "==> upload s3://$BUCKET/$KEY" >&2
if ! docker save "kb-mcp:$TAG" | gzip | aws s3 cp - "s3://$BUCKET/$KEY" \
  --region "$REGION" --sse AES256 >&2; then
  echo "upload failed; removing any partial object" >&2
  aws s3 rm "s3://$BUCKET/$KEY" --region "$REGION" >&2 || true
  exit 1
fi

# Confirm the object exists and is not empty.
UPLOADED="$(aws s3api head-object \
  --region "$REGION" --bucket "$BUCKET" --key "$KEY" \
  --query ContentLength --output text)"
if [ -z "$UPLOADED" ] || [ "$UPLOADED" = "None" ] || [ "$UPLOADED" -le 0 ]; then
  echo "s3://$BUCKET/$KEY is missing or empty after the upload" >&2
  exit 1
fi
echo "uploaded: s3://$BUCKET/$KEY ($UPLOADED bytes compressed)" >&2

SIZE="$(docker image inspect "$LOCAL_IMAGE" --format '{{.Size}}')"
echo "image size: $SIZE bytes" >&2

echo "$TAG"
