#!/usr/bin/env bash
# Deploy one kb-mcp CloudFormation stack.
#
#   infra/deploy.sh network
#   infra/deploy.sh data
#   infra/deploy.sh app <ImageTag>
#
# Each run lints the template, validates it with CloudFormation, then deploys it
# as stack kb-<name>. Deploy order: network, then data, then app.
#
# The app stack needs two values from the environment (the script stops with an
# error if one is missing):
#   KB_CLOUDFRONT_PREFIX_LIST_ID  CloudFront origin-facing prefix list (pl-...),
#                                 recorded in docs/PROGRESS.md in phase 00
#   KB_ALERT_EMAIL                address for the alarm emails
# and takes two optional values:
#   KB_METRICS_MODE               emf (default) or api, how the app publishes
#                                 search metrics (see kb/metrics.py)
#   KB_EXTENSION_ORIGIN           chrome-extension://<32 letter id>, the only origin
#                                 CORS allows; unset or empty means no CORS headers
set -euo pipefail

REGION="us-east-1"
TAGS="Project=kb-mcp"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: infra/deploy.sh network|data|app [ImageTag]" >&2
  echo "  app requires an ImageTag (the tag pushed by infra/push-image.sh)" >&2
  echo "  app also needs KB_CLOUDFRONT_PREFIX_LIST_ID and KB_ALERT_EMAIL in the environment" >&2
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
  app)
    [ $# -eq 2 ] || usage
    require_env KB_CLOUDFRONT_PREFIX_LIST_ID "the CloudFront origin-facing prefix list ID (pl-..., see docs/PROGRESS.md, phase 00)"
    require_env KB_ALERT_EMAIL "the email address that receives the alarm emails"
    deploy_stack app --parameter-overrides \
      "ImageTag=$2" \
      "CloudFrontPrefixListId=$KB_CLOUDFRONT_PREFIX_LIST_ID" \
      "AlertEmail=$KB_ALERT_EMAIL" \
      "MetricsMode=${KB_METRICS_MODE:-emf}" \
      "ExtensionOrigin=${KB_EXTENSION_ORIGIN:-}"
    print_output AlbDnsName "ALB DNS name"
    print_output DistributionDomainName "CloudFront domain name"
    echo "Confirm the SNS subscription email sent to $KB_ALERT_EMAIL (alarm emails only arrive after that)."
    ;;
  *)
    usage
    ;;
esac
