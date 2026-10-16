#!/usr/bin/env bash
# Restore the kb database from S3. Run as root ON THE INSTANCE.
#
#   restore-on-instance.sh --latest
#   restore-on-instance.sh --key backups/kb-20261011-071500.db.gz
#
# --latest means the snapshot (snapshots/kb-latest.db.gz), else the newest nightly
# backup, else nothing. The snapshot is the app's current state: stopping the app
# below uploads a last snapshot of it first, so --latest after a bad delete restores
# the bad delete. To go back to an earlier state use --key with a nightly backup.
#
# What it does:
#   1. reads the image and settings from the running "kb" container,
#   2. stops the container (replacing the database file under a running app
#      corrupts its view of it),
#   3. runs `python -m kb.cli restore ... --force` in a one-off container with
#      the same image, the same /data mount and the same bucket,
#   4. starts "kb" again, whatever the restore did (a failed restore leaves the
#      old database in place, see kb/backup.py), and polls /healthz.
#
# The old database stays next to the new one as kb.db.pre-restore-<timestamp>.
# The exit code is non-zero if the restore or the health check failed.
#
# The instance has no copy of the repository. To run this from a laptop with
# Session Manager's send-command (not tried yet), send the file as base64:
#   B64=$(base64 -w0 infra/scripts/restore-on-instance.sh)
#   aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript \
#     --parameters "commands=[\"echo $B64 | base64 -d > /tmp/restore.sh\",\"bash /tmp/restore.sh --latest\"]"
set -euo pipefail

CONTAINER="${KB_CONTAINER:-kb}"
HEALTH_URL="${KB_HEALTH_URL:-http://localhost:8000/healthz}"
HEALTH_TIMEOUT="${KB_HEALTH_TIMEOUT:-600}"

usage() {
  echo "usage: restore-on-instance.sh --latest | --key backups/kb-YYYYMMDD-HHMMSS.db.gz" >&2
  echo "  --latest takes the snapshot, else the newest backup; use --key for an earlier backup" >&2
  exit 2
}

case "${1:-}" in
  --latest)
    [ $# -eq 1 ] || usage
    restore_args=(--latest)
    ;;
  --key)
    if [ $# -ne 2 ] || [ -z "$2" ]; then
      usage
    fi
    restore_args=(--key "$2")
    ;;
  *)
    usage
    ;;
esac

# env_of NAME: value of NAME in the container's environment (empty if unset).
env_of() {
  local all
  all=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
    | sed -n "s/^$1=//p")
  printf '%s\n' "${all%%$'\n'*}"
}

IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER")
KB_DB=$(env_of KB_DB)
KB_BUCKET=$(env_of KB_BUCKET)
AWS_REGION=$(env_of AWS_REGION)
if [ -z "$KB_DB" ] || [ -z "$KB_BUCKET" ]; then
  echo "container $CONTAINER has no KB_DB or KB_BUCKET; refusing to continue" >&2
  exit 1
fi

# Whatever happens after the stop, bring the app back.
restart_container() {
  echo "==> starting $CONTAINER"
  docker start "$CONTAINER" > /dev/null
}

echo "==> stopping $CONTAINER"
docker stop "$CONTAINER" > /dev/null
trap restart_container EXIT

echo "==> restoring ${restore_args[*]} into $KB_DB (image $IMAGE)"
status=0
docker run --rm \
  --volumes-from "$CONTAINER" \
  -e "KB_DB=$KB_DB" \
  -e "KB_BUCKET=$KB_BUCKET" \
  -e "AWS_REGION=${AWS_REGION:-us-east-1}" \
  "$IMAGE" \
  python -m kb.cli restore "${restore_args[@]}" --force || status=$?

# Start now (the EXIT trap would be too late for the health poll) and do not
# start it a second time.
trap - EXIT
restart_container

echo "==> waiting up to ${HEALTH_TIMEOUT}s for $HEALTH_URL"
deadline=$((SECONDS + HEALTH_TIMEOUT))
healthy=0
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -sf "$HEALTH_URL" > /dev/null; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "$status" -ne 0 ]; then
  echo "restore FAILED (exit $status); the previous database is still in place" >&2
  exit "$status"
fi
if [ "$healthy" -ne 1 ]; then
  echo "restore finished but the app did not become healthy within ${HEALTH_TIMEOUT}s" >&2
  docker logs --tail 50 "$CONTAINER" >&2 || true
  exit 1
fi
echo "==> restore complete, app healthy"
curl -s "$HEALTH_URL"
echo
