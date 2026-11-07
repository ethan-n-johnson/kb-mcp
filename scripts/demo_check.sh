#!/usr/bin/env bash
# Pre-demo check of the live stack. Read only: nothing is stored, changed or deleted.
#
#   scripts/demo_check.sh <cloudfront domain>      (https:// is added)
#
#   scripts/demo_check.sh https://<cloudfront domain>
#
# Prints PASS, FAIL or SKIP per line and exits 1 if any line is FAIL.
# Needs curl and jq. The AWS CLI (credentials for the sandbox account) adds two
# checks: the stack runs in postgres mode and 2 or more targets are healthy; without
# it those lines are SKIP.
#
# Two things the check cannot avoid: the out-of-corpus search is logged as a gap
# (its text starts with "demo check:", so it is easy to spot in /api/gaps), and the
# searches count against the per-address rate limit (a handful of requests).
set -u

BASE="${1:-}"
if [ -z "$BASE" ]; then
  echo "usage: scripts/demo_check.sh https://<cloudfront domain>" >&2
  exit 2
fi
BASE="${BASE%/}"
# A bare domain (the stack output, "$CF") means https: CloudFront answers plain
# http with a 301 redirect, which these curl calls do not follow.
case "$BASE" in
  http://*|https://*) ;;
  *) BASE="https://$BASE" ;;
esac
REGION="us-east-1"
FAILS=0

for tool in curl jq; do
  command -v "$tool" > /dev/null 2>&1 || {
    echo "error: $tool is required" >&2
    exit 2
  }
done

pass() { printf 'PASS  %s\n' "$1"; }
skip() { printf 'SKIP  %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1"
  FAILS=$((FAILS + 1))
}

# get PATH [QUERY...]: GET with the query parameters URL-encoded, 30 s limit.
get() {
  local path=$1
  shift
  local args=()
  for kv in "$@"; do args+=(--data-urlencode "$kv"); done
  curl -s -m 30 -G "${args[@]}" "$BASE$path"
}

# mcp JSON: POST one JSON-RPC request to /mcp with no Authorization header.
mcp() {
  curl -s -m 30 -X POST "$BASE/mcp" \
    -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
    -d "$1"
}

# --- health ----------------------------------------------------------------
resp="$(curl -s -m 15 -w '\n%{http_code}' "$BASE/healthz")"
health_code="${resp##*$'\n'}"
health="${resp%$'\n'*}"
if [ "$health_code" = "200" ] && [ "$(jq -r .status <<< "$health" 2> /dev/null)" = "ok" ]; then
  pass "/healthz is 200 ok ($(jq -r '"\(.chunks) chunks"' <<< "$health"))"
else
  fail "/healthz returned HTTP $health_code: $health"
fi

mode="$(jq -r .mode <<< "$health" 2> /dev/null)"
if [ "$mode" = "semantic" ]; then pass "mode is semantic"; else fail "mode is '$mode', expected semantic (is the fallback toggle on, or a model not loaded?)"; fi

if [ "$(jq -r '.models.embedder + " " + .models.reranker' <<< "$health" 2> /dev/null)" = "ok ok" ]; then
  pass "both models are ok"
else
  fail "models: $(jq -c .models <<< "$health" 2> /dev/null)"
fi

if [ "$(jq -r .fallback_toggle <<< "$health" 2> /dev/null)" = "false" ]; then
  pass "fallback toggle is off"
else
  fail "fallback toggle is on (turn it off before the demo)"
fi

# --- stack: postgres mode and healthy targets (AWS CLI) -------------------------
if command -v aws > /dev/null 2>&1 && aws sts get-caller-identity > /dev/null 2>&1; then
  dbmode="$(aws cloudformation describe-stacks --region "$REGION" --stack-name kb-app \
    --query "Stacks[0].Parameters[?ParameterKey=='DatabaseMode'].ParameterValue" --output text 2> /dev/null)"
  if [ "$dbmode" = "postgres" ]; then pass "kb-app runs in postgres mode"; else fail "kb-app DatabaseMode is '${dbmode:-unknown}', expected postgres"; fi

  tg="$(aws cloudformation describe-stacks --region "$REGION" --stack-name kb-app \
    --query "Stacks[0].Outputs[?OutputKey=='TargetGroupArn'].OutputValue" --output text 2> /dev/null)"
  if [ -n "$tg" ] && [ "$tg" != "None" ]; then
    healthy="$(aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$tg" \
      --query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text 2> /dev/null)"
    if [ "${healthy:-0}" -ge 2 ] 2> /dev/null; then pass "$healthy healthy targets (2 or more needed)"; else fail "healthy targets: ${healthy:-unknown}, expected 2 or more"; fi
  else
    fail "could not find the kb-app target group"
  fi
else
  skip "stack mode and healthy targets (AWS CLI missing or not logged in)"
fi

# --- the two demo searches and one out-of-corpus search --------------------------
check_search() {
  local label=$1 want=$2 query=$3 body got conf top
  body="$(get /api/search "q=$query")"
  got="$(jq -r '.mode + " " + .confidence' <<< "$body" 2> /dev/null)"
  conf="${got#* }"
  top="$(jq -r '.results[0].heading_path // "no result"' <<< "$body" 2> /dev/null)"
  if [ "$conf" = "$want" ]; then
    pass "$label: confidence $conf, mode ${got%% *} (top: $top)"
  else
    fail "$label: got '${got:-no answer}', expected confidence $want"
  fi
}
check_search "demo search 1 (Auto Scaling)" high "how does an Auto Scaling group replace an unhealthy instance"
check_search "demo search 2 (EFS mount)" high "how do I mount EFS on Amazon Linux"
check_search "out-of-corpus search" low "demo check: how do I bake a sourdough loaf with rye flour"

# --- stale and gaps -------------------------------------------------------------
stale="$(get /api/stale)"
planted=0
for part in AmazonS3/latest/userguide/Versioning.html AmazonCloudFront/latest/DeveloperGuide/Introduction.html tutorial/debugging.md; do
  jq -e --arg p "$part" '[.docs[].source_url // ""] | any(endswith($p))' <<< "$stale" > /dev/null 2>&1 && planted=$((planted + 1))
done
if [ "$planted" -eq 3 ]; then pass "/api/stale lists the 3 planted docs"; else fail "/api/stale shows $planted of the 3 planted docs"; fi

gaps_code="$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$BASE/api/gaps")"
if [ "$gaps_code" = "200" ]; then pass "/api/gaps answers 200"; else fail "/api/gaps returned HTTP $gaps_code"; fi

# --- MCP ------------------------------------------------------------------------
tools="$(mcp '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length' 2> /dev/null)"
if [ "$tools" = "12" ]; then pass "MCP tools/list returns 12 tools"; else fail "MCP tools/list returned '${tools:-nothing}' tools, expected 12"; fi

# A delete of a document id that does not exist, sent without a token, must be refused with the token error. If the gate were open it would answer not_found (and still delete nothing).
refusal="$(mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delete","arguments":{"doc_id":999999999}}}' |
  jq -r 'if .result.isError then .result.content[0].text else "not refused" end' 2> /dev/null)"
case "$refusal" in
  *"needs a write token"*) pass "MCP write without a token is refused (nothing written)" ;;
  *) fail "MCP delete without a token answered: ${refusal:-nothing}" ;;
esac

# --- static asset cache ---------------------------------------------------------
cache="$(curl -s -m 15 -I "$BASE/static/app.css" | tr -d '\r' | grep -i '^cache-control:' | head -1)"
if [ -n "$cache" ]; then pass "static asset has ${cache}"; else fail "/static/app.css has no Cache-Control header"; fi

echo
if [ "$FAILS" -eq 0 ]; then
  echo "All checks passed."
  exit 0
fi
echo "$FAILS check(s) failed."
exit 1
