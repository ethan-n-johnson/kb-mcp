#!/usr/bin/env bash
# Deploy one kb-mcp CloudFormation stack.
#
#   infra/deploy.sh network
#   infra/deploy.sh data
#   infra/deploy.sh db
#   infra/deploy.sh app <ImageTag>
#
# Each run lints the template, validates it with CloudFormation, then deploys it
# as stack kb-<name>. Deploy order: network, data, db, then app.
#
# The db stack needs the master password in the environment (the script stops with
# an error if it is missing; it is never printed):
#   KB_DB_PASSWORD                16 to 64 letters and digits (infra/up.sh keeps it
#                                 in ~/.config/kb-mcp/db-password)
# and takes four optional values, passed only when set (the template defaults are
# db.t3.micro, 16.9, Multi-AZ on, encrypted):
#   KB_DB_CLASS                   DbInstanceClass
#   KB_DB_VERSION                 EngineVersion
#   KB_DB_MULTI_AZ                true or false (MultiAz)
#   KB_DB_ENCRYPTED               true or false (StorageEncrypted)
#
# The app stack needs two values from the environment (the script stops with an
# error if one is missing):
#   KB_CLOUDFRONT_PREFIX_LIST_ID  CloudFront origin-facing prefix list (pl-...),
#                                 recorded in docs/PROGRESS.md in phase 00
#   KB_ALERT_EMAIL                address for the alarm emails
# and takes four optional values:
#   KB_INSTANCE_PROFILE           existing instance profile for the app instances,
#                                 default myS3Role (the only profile the Cloud
#                                 Architecting sandbox lets you pass; Learner Lab
#                                 has LabInstanceProfile)
#   KB_METRICS_MODE               api (default) or emf, how the app publishes
#                                 search metrics (see kb/metrics.py); emf needs an
#                                 instance role that can write CloudWatch Logs
#   KB_EXTENSION_ORIGIN           chrome-extension://<32 letter id>, the only origin
#                                 CORS allows; unset or empty means no CORS headers
#   KB_DATABASE_MODE              sqlite (default) or postgres, where the knowledge
#                                 base lives; postgres needs the kb-db stack (the
#                                 instance joins kb-DbClientSg and imports the S3
#                                 snapshot into an empty RDS database at boot)
set -euo pipefail

REGION="us-east-1"
TAGS="Project=kb-mcp"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: infra/deploy.sh network|data|db|app [ImageTag]" >&2
  echo "  app requires an ImageTag (the tag pushed by infra/push-image.sh)" >&2
  echo "  app also needs KB_CLOUDFRONT_PREFIX_LIST_ID and KB_ALERT_EMAIL in the environment" >&2
  echo "  db needs KB_DB_PASSWORD in the environment" >&2
  exit 2
}

# Prefer cfn-lint on PATH, then the repo virtualenv.
find_cfn_lint() {
  if command -v cfn-lint > /dev/null 2>&1; then
    command -v cfn-lint
  elif [ -x "$ROOT/.venv/bin/cfn-lint" ]; then
    echo "$ROOT/.venv/bin/cfn-lint"
  else
    echo "cfn-lint not found: pip install -e '.[dev]' in the repo virtualenv" >&2
    return 1
  fi
}

# deploy_stack NAME [extra aws cloudformation deploy args...]
deploy_stack() {
  local name=$1
  shift
  local template="$ROOT/infra/$name.yaml"
  local stack="kb-$name"
  local lint
  lint="$(find_cfn_lint)"

  echo "==> cfn-lint $template"
  "$lint" "$template"

  echo "==> validate-template $template"
  aws cloudformation validate-template \
    --region "$REGION" \
    --template-body "file://$template" > /dev/null

  echo "==> deploy $stack"
  aws cloudformation deploy \
    --region "$REGION" \
    --stack-name "$stack" \
    --template-file "$template" \
    --no-fail-on-empty-changeset \
    --tags "$TAGS" \
    "$@"
}

# require_env NAME DESCRIPTION: stop with a clear message if NAME is unset or empty.
require_env() {
  local name=$1 what=$2
  if [ -z "${!name:-}" ]; then
    echo "error: $name is not set; export it as $what" >&2
    exit 2
  fi
}

# print_output OUTPUT_KEY LABEL: print one output of the kb-app stack.
print_output() {
  echo "==> $2"
  aws cloudformation describe-stacks \
    --region "$REGION" \
    --stack-name kb-app \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" \
    --output text
}

[ $# -ge 1 ] || usage

case "$1" in
  network | data)
    [ $# -eq 1 ] || usage
    deploy_stack "$1"
    ;;
  db)
    [ $# -eq 1 ] || usage
    require_env KB_DB_PASSWORD "the database master password (16 to 64 letters and digits; infra/up.sh keeps it in ~/.config/kb-mcp/db-password)"
    db_params=("DbPassword=$KB_DB_PASSWORD")
    [ -z "${KB_DB_CLASS:-}" ] || db_params+=("DbInstanceClass=$KB_DB_CLASS")
    [ -z "${KB_DB_VERSION:-}" ] || db_params+=("EngineVersion=$KB_DB_VERSION")
    [ -z "${KB_DB_MULTI_AZ:-}" ] || db_params+=("MultiAz=$KB_DB_MULTI_AZ")
    [ -z "${KB_DB_ENCRYPTED:-}" ] || db_params+=("StorageEncrypted=$KB_DB_ENCRYPTED")
    deploy_stack db --parameter-overrides "${db_params[@]}"
    ;;
  app)
    [ $# -eq 2 ] || usage
    require_env KB_CLOUDFRONT_PREFIX_LIST_ID "the CloudFront origin-facing prefix list ID (pl-..., see docs/PROGRESS.md, phase 00)"
    require_env KB_ALERT_EMAIL "the email address that receives the alarm emails"
    case "${KB_DATABASE_MODE:-sqlite}" in
      sqlite | postgres) ;;
      *)
        echo "error: KB_DATABASE_MODE must be sqlite or postgres" >&2
        exit 2
        ;;
    esac
    deploy_stack app --parameter-overrides \
      "ImageTag=$2" \
      "CloudFrontPrefixListId=$KB_CLOUDFRONT_PREFIX_LIST_ID" \
      "AlertEmail=$KB_ALERT_EMAIL" \
      "MetricsMode=${KB_METRICS_MODE:-api}" \
      "InstanceProfileName=${KB_INSTANCE_PROFILE:-myS3Role}" \
      "ExtensionOrigin=${KB_EXTENSION_ORIGIN:-}" \
      "DatabaseMode=${KB_DATABASE_MODE:-sqlite}"
    print_output AlbDnsName "ALB DNS name"
    print_output DistributionDomainName "CloudFront domain name"
    echo "Confirm the SNS subscription email sent to $KB_ALERT_EMAIL (alarm emails only arrive after that)."
    ;;
  *)
    usage
    ;;
esac
