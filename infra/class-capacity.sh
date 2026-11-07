#!/usr/bin/env bash
# Have 4 healthy app instances when class starts, and 2 again afterwards.
#
#   infra/class-capacity.sh up                        MinSize 4, DesiredCapacity 4 now
#   infra/class-capacity.sh down                      MinSize 2, DesiredCapacity 2 now
#   infra/class-capacity.sh schedule START END        two scheduled actions (UTC):
#                                                     kb-class-up at START minus 20
#                                                     minutes, kb-class-down at END
#   infra/class-capacity.sh status                    sizes, in service count, healthy
#                                                     targets, scheduled actions
#
# START and END look like 2026-11-12T14:00:00Z (UTC, the Z is required).
#
# Why: scale-out through CPU target tracking is too slow once 40 students search at
# the same time: the alarm needs minutes of load first (3 to 12 minutes in phases 23
# and 23a) and then a new instance needs time to load the image and models (phase 23
# saw about 10 minutes under load). Raising the group's minimum makes Auto Scaling
# launch the missing instances at once, in parallel, before anyone searches. Trial on
# 2026-11-07 with an idle stack: 2 extra instances launched and 4 healthy targets after
# 2 min 50 s. Keep a margin anyway: start it at least 20 minutes before class (the
# schedule does).
#
# Rules:
#   - Only in DatabaseMode=postgres (the sqlite stack is exactly 1 instance).
#   - MaxSize is never changed and never above 4 (the sandbox suspends the account
#     past 9 instances). The script refuses when the group's MaxSize is not 4.
#   - A stack update (infra/up.sh, infra/deploy.sh app) resets MinSize and
#     DesiredCapacity to the template's 2. Do not redeploy while scaled up; if you
#     must, run "up" again afterwards. The scheduled actions survive an update.
#   - "down" sets the minimum back to 2; CPU target tracking then scales in by
#     itself, one instance at a time, so the group reaches 2 within a few minutes
#     (the scheduled kb-class-down does the same at END).
#   - 4 instances cost double 2 instances: use it for the class only.
set -euo pipefail

REGION="us-east-1"
STACK="kb-app"
UP_MIN=4
DOWN_MIN=2
LEAD_MINUTES=20

die() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  echo "usage: infra/class-capacity.sh up | down | status | schedule <start-utc> <end-utc>" >&2
  echo "       times look like 2026-11-12T14:00:00Z" >&2
  exit 2
}

stack_output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2> /dev/null || true
}

# Sets ASG (the group name) and refuses anything but a postgres stack with MaxSize 4.
load_group() {
  local mode max
  mode="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='DatabaseMode'].ParameterValue" --output text 2> /dev/null || true)"
  [ "$mode" = "postgres" ] || die "$STACK must exist in DatabaseMode=postgres (found: ${mode:-no stack})"
  ASG="$(stack_output AsgName)"
  { [ -n "$ASG" ] && [ "$ASG" != "None" ]; } || die "output AsgName of $STACK not found"
  max="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
    --auto-scaling-group-names "$ASG" --query 'AutoScalingGroups[0].MaxSize' --output text)"
  [ "$max" = "$UP_MIN" ] || die "the group's MaxSize is $max, expected $UP_MIN; not touching it"
}

set_capacity() {
  aws autoscaling update-auto-scaling-group --region "$REGION" --auto-scaling-group-name "$ASG" \
    --min-size "$1" --desired-capacity "$1"
}

# check_time VALUE: a UTC timestamp ending in Z; prints it in epoch seconds.
check_time() {
  local re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?Z$'
  [[ "$1" =~ $re ]] || die "bad time '$1': use UTC like 2026-11-12T14:00:00Z"
  date -u -d "$1" +%s 2> /dev/null || die "bad time '$1'"
}

show_status() {
  local tg
  echo "group $ASG"
  aws autoscaling describe-auto-scaling-groups --region "$REGION" --auto-scaling-group-names "$ASG" \
    --query 'AutoScalingGroups[0].{Min:MinSize,Max:MaxSize,Desired:DesiredCapacity,InService:length(Instances[?LifecycleState==`InService`])}' \
    --output table
  tg="$(stack_output TargetGroupArn)"
  if [ -n "$tg" ] && [ "$tg" != "None" ]; then
    echo "targets"
    aws elbv2 describe-target-health --region "$REGION" --target-group-arn "$tg" \
      --query 'TargetHealthDescriptions[].{Id:Target.Id,State:TargetHealth.State}' --output table
  fi
  echo "scheduled actions"
  aws autoscaling describe-scheduled-actions --region "$REGION" --auto-scaling-group-name "$ASG" \
    --query 'ScheduledUpdateGroupActions[].{Name:ScheduledActionName,Start:StartTime,Min:MinSize,Desired:DesiredCapacity}' \
    --output table
}

[ $# -ge 1 ] || usage
cmd=$1
case "$cmd" in
  up | down | status)
    [ $# -eq 1 ] || usage
    ;;
  schedule)
    [ $# -eq 3 ] || usage
    ;;
  *)
    usage
    ;;
esac

load_group

case "$cmd" in
  up)
    set_capacity "$UP_MIN"
    echo "Asked for $UP_MIN instances. Expect 4 healthy targets in 3 to 10 minutes;"
    echo "watch with: infra/class-capacity.sh status"
    ;;
  down)
    set_capacity "$DOWN_MIN"
    echo "Minimum and desired capacity set to $DOWN_MIN; CPU target tracking scales in from here."
    ;;
  status)
    show_status
    ;;
  schedule)
    start_epoch="$(check_time "$2")"
    end_epoch="$(check_time "$3")"
    [ "$end_epoch" -gt "$start_epoch" ] || die "END must be after START"
    up_time="$(date -u -d "@$((start_epoch - LEAD_MINUTES * 60))" +%Y-%m-%dT%H:%M:%SZ)"
    down_time="$(date -u -d "@$end_epoch" +%Y-%m-%dT%H:%M:%SZ)"
    aws autoscaling put-scheduled-update-group-action --region "$REGION" --auto-scaling-group-name "$ASG" \
      --scheduled-action-name kb-class-up --start-time "$up_time" \
      --min-size "$UP_MIN" --desired-capacity "$UP_MIN"
    aws autoscaling put-scheduled-update-group-action --region "$REGION" --auto-scaling-group-name "$ASG" \
      --scheduled-action-name kb-class-down --start-time "$down_time" \
      --min-size "$DOWN_MIN" --desired-capacity "$DOWN_MIN"
    echo "kb-class-up at $up_time ($LEAD_MINUTES minutes before $2), kb-class-down at $down_time."
    echo "Remove them with: aws autoscaling delete-scheduled-action --auto-scaling-group-name $ASG --scheduled-action-name kb-class-up (and kb-class-down)"
    ;;
esac
