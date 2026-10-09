# Phase 15: Monitoring and alarms

**Day:** 6 (Mon Oct 12) · **Depends on:** 12 · **Where:** AWS · **PLAN.md sections:** Search (metrics and alarm sentence); Course alignment (Monitoring row); Well-Architected (Operational excellence); deviation 3 in PROGRESS.md

## Goal

Search metrics, host memory and disk metrics, alarms, and email alerts through SNS. The keyword-mode alarm email was part of demo step 7; in the sandbox the instance role cannot publish custom metrics, so step 7 uses the `kb-no-healthy-host` alarm during an instance replacement (deviation 8).

**Sandbox (deviation 8 in PROGRESS.md):** the instance role cannot write CloudWatch Logs, so there is no log group and no `awslogs` driver. Container logs stay on the instance (Docker `local` driver), and search metrics are sent with `put_metric_data` (`MetricsMode=api`, the default). Tasks 1, 2, 3 and 4a below describe the original design and are replaced by that.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (deviations 3 and 8)
- `docs/PLAN.md`: the metrics paragraph in "Search", Monitoring row in "Course alignment"

## Tasks

1. `infra/app.yaml`:
   - Parameter `AlertEmail`.
   - `AWS::Logs::LogGroup` `/kb/app`, retention 14 days.
   - `AWS::SNS::Topic` with an email subscription to `AlertEmail`.
2. User data (replace the phase 06 placeholders):
   - Run the container with `--log-driver awslogs --log-opt awslogs-region=<Region> --log-opt awslogs-group=/kb/app --log-opt awslogs-stream=<instance-id>` (get the instance ID from IMDSv2 in the script).
   - Install `amazon-cloudwatch-agent`; write a config that collects `mem_used_percent` and `disk_used_percent` (path `/`) with `append_dimensions` `AutoScalingGroupName` and `InstanceId`, plus `aggregation_dimensions [["AutoScalingGroupName"]]`; start it with `fetch-config -m ec2 -s`. The disk path `/` matters more than it did with a shared file system: the live database, Docker's image layers and the logs all sit on the 30 GB root volume.
3. Confirm the EMF metrics arrive: after deploying, run 5 searches and 1 keyword-mode search, then check `aws cloudwatch list-metrics --namespace KbMcp` within about 5 minutes. **If they do not appear**, the logs are not being parsed as EMF; switch `kb/metrics.py` to send the same three metrics with boto3 `put_metric_data` from a background thread that batches every 30 seconds, and record the deviation. Do not spend more than one attempt on making EMF work.
4. Alarms (all with `AlarmActions` and `OKActions` to the SNS topic, `TreatMissingData: notBreaching` unless stated):
   - `kb-keyword-mode`: `KbMcp` `KeywordSearches` (dimension `Mode=keyword`), Sum ≥ 1 over 1 minute, 1 period.
   - `kb-unhealthy-host`: `AWS/ApplicationELB` `UnHealthyHostCount` (dimensions `TargetGroup`, `LoadBalancer` from the phase 06 outputs), Max ≥ 1 for 2 of 2 one-minute periods.
   - `kb-no-healthy-host`: `HealthyHostCount` Min < 1 for 3 of 3 one-minute periods, `TreatMissingData: breaching`.
   - `kb-5xx`: `HTTPCode_Target_5XX_Count` Sum ≥ 5 over 5 minutes.
   - `kb-high-latency`: `KbMcp` `LatencyMs` p90 > 2000 over 5 minutes.
   - `kb-memory`: `CWAgent` `mem_used_percent` (dimension `AutoScalingGroupName`) Avg > 85 for 2 of 2 five-minute periods.
4a. Snapshot health is visible in the same log group: `aws logs filter-log-events --log-group-name /kb/app --filter-pattern '"snapshot uploaded"'` shows one line per snapshot with `key`, `bytes` and `ms`, and `'"snapshot failed"'` shows failed uploads (retried one interval later). There is no alarm on it (a `SnapshotAgeSeconds` metric was left out on purpose); record in "Open issues" if failures show up.
5. Optional, small: a CloudWatch dashboard `kb-mcp` with search latency, mode counts, healthy hosts and memory. Useful on the architecture slide; skip if short on time.
6. Deploy with the email. **HUMAN:** confirm the SNS subscription from the inbox.
7. Alarm test: turn the fallback toggle on, run a search, wait for the `kb-keyword-mode` email (**HUMAN** confirms it arrived), then turn the toggle off and wait for the OK email.

## Do not

- Create IAM roles or policies (IAM is read only; the instance role `myS3Role` cannot publish metrics, so the `KbMcp` and `CWAgent` alarms get no data in this sandbox, deviation 8).
- Alarm on things the demo deliberately triggers without saying so in the alarm description.

## Acceptance

- [ ] `aws cloudwatch list-metrics --namespace KbMcp` and `--namespace CWAgent` both return metrics.
- [ ] After an upload, `aws s3api head-object --bucket <bucket> --key snapshots/kb-latest.db.gz` shows a `LastModified` within the last minute.
- [ ] All six alarms exist and are `OK` (or `INSUFFICIENT_DATA` for the latency one before traffic).
- [ ] The user received an alarm email and the OK email (in the sandbox: `kb-no-healthy-host`, deviation 8).
- [ ] Phase log updated; commit `phase 15: monitoring and alarms`.
