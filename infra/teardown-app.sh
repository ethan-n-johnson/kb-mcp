#!/usr/bin/env bash
# Delete the kb-app stack only (NAT gateway, ALB, instance). The kb-network and
# kb-data stacks, and therefore the S3 bucket with the database snapshots and
# backups, are left alone. The database on the instance's root volume goes with the
# instance; the next deploy restores it from the S3 snapshot.
# Asks for confirmation first. Recreate with: infra/deploy.sh app <ImageTag>
set -euo pipefail

REGION="us-east-1"
STACK="kb-app"

if ! aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" > /dev/null 2>&1; then
  echo "Stack $STACK does not exist (or is not visible). Nothing to delete."
  exit 0
fi

echo "This will delete the CloudFormation stack $STACK in $REGION:"
echo "  NAT gateway, Elastic IP, ALB, target group, launch template, Auto Scaling group."
echo "  The instance is terminated (its docker stop uploads a last database snapshot)."
echo "  The S3 bucket in kb-data (snapshots, backups, images) is kept."
printf 'Type the stack name (%s) to confirm: ' "$STACK"
read -r answer
if [ "$answer" != "$STACK" ]; then
  echo "Aborted."
  exit 1
fi

aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
echo "Waiting for $STACK to be deleted..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Deleted $STACK."
