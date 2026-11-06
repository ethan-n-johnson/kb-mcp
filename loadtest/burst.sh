#!/usr/bin/env bash
# Quick reproduction check (phase 23a, B2): 4 parallel loops of 25 searches each.
# Usage: loadtest/burst.sh <host>   e.g. http://localhost:8000 or https://<cloudfront domain>
# Prints how many responses came back per HTTP status code (000 means no
# response: connection error or timeout) and the slowest request.
# Expect 0 x 502 and 0 x 000. 429 is fine when the rate limit is the default.
set -u

HOST="${1:?usage: burst.sh <host>}"
HOST="${HOST%/}"
LOOPS=4
PER_LOOP=25

QUESTIONS=(
  "how does an Auto Scaling group replace an unhealthy instance"
  "how do I attach an EBS volume to an EC2 instance"
  "what is the health check grace period for an Auto Scaling group"
  "how do I require IMDSv2 for instance metadata"
  "how do NAT gateways let private subnets reach the internet"
  "how do I enable versioning on an S3 bucket"
  "how do I preview CloudFormation stack changes with a change set"
  "how do I enable CORS in FastAPI with CORSMiddleware"
  "what is the Streamable HTTP transport in MCP"
  "how does an MCP server expose tools to clients"
  "my web server died, will the fleet bring up a fresh one by itself"
  "keep older copies of files around after someone overwrites them"
  "run a script automatically the first time a virtual machine boots"
  "which FastAPI setting lets a browser page on another origin call my API"
)

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

worker() {
  local id="$1" i q
  for ((i = 0; i < PER_LOOP; i++)); do
    q="${QUESTIONS[$(((id * 7 + i) % ${#QUESTIONS[@]}))]}"
    curl -s -m 30 -o /dev/null -w '%{http_code} %{time_total}\n' \
      -G --data-urlencode "q=${q}" "${HOST}/api/search" >>"$OUT/loop$id" || true
  done
}

for ((n = 0; n < LOOPS; n++)); do
  worker "$n" &
done
wait

cat "$OUT"/loop* | awk '
  { count[$1]++; if ($2 + 0 > max) max = $2 + 0; total++ }
  END {
    for (c in count) printf "status %s: %d\n", c, count[c]
    printf "total: %d\nmax time: %.2f s\n", total, max
  }' | sort
