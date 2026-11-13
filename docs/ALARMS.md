# Alarms and monitoring runbook

Every CloudWatch alarm in `infra/app.yaml`, what it means and what to do. Written in phase 25.6 (2026-11-08). All alarms notify the SNS topic `kb-alerts` (email) when they go to ALARM and again when they return to OK.

## Why these alarms

The sandbox instance role (`myS3Role`) can use S3 and nothing else, so the app cannot publish custom metrics or write CloudWatch Logs (deviation 8). Alarms on custom metrics would never receive data and would stay OK forever. So the alarms read metrics that AWS publishes by itself: the load balancer (`AWS/ApplicationELB`), the Auto Scaling group (`AWS/AutoScaling`, with group metrics collection on), EC2 (`AWS/EC2`) and RDS (`AWS/RDS`). The three custom-metric alarms exist only when `MetricsMode` is `api` or `emf`, which needs an instance role that may publish metrics.

## The alarms

| Alarm | Mode | Metric | Fires when | Missing data | Likely cause | What to do |
| --- | --- | --- | --- | --- | --- | --- |
| `kb-no-healthy-host` | both | ALB `HealthyHostCount` (min) | Fewer than 1 healthy target for 3 minutes | Breaching (no targets publishes nothing) | Every instance failed `/livez`, or none is registered (boot failing, group at 0) | `aws elbv2 describe-target-health`; `aws autoscaling describe-scaling-activities`; check `s3://<bucket>/debug/` for failed boot logs. Users are seeing 503s. |
| `kb-unhealthy-host` | both | ALB `UnHealthyHostCount` (max) | At least 1 unhealthy target for 2 minutes | OK | One instance stopped answering `/livez` (process down or stuck); also expected for a minute while a new instance joins | If it lasts, the group replaces the instance (ELB health checks). Check scaling activities for "ELB system health check failure". Expected during the failover demo. |
| `kb-5xx` | both | ALB `HTTPCode_Target_5XX_Count` (sum) | 5 or more 5xx from the app in 5 minutes | OK | Unhandled errors, an S3 failure on export (502 `bad_gateway`), the database unreachable from an instance | `curl https://<CloudFront>/healthz` (a 503 there means the database is not readable); container logs are only on the instance. Since phase 25.3 an instance with broken database access stays in service, so this alarm is how that shows up. |
| `kb-elb-5xx` | both | ALB `HTTPCode_ELB_5XX_Count` (sum) | 5 or more 5xx produced by the load balancer itself in 5 minutes | OK | 502: a target closed the connection (crash, restart); 503: no healthy target; 504: a target took over 60 s | Look at `kb-no-healthy-host` and `kb-unhealthy-host` first. Phase 23 run1 (76 percent 502s) is the example this alarm would have caught; `kb-5xx` did not. |
| `kb-response-time` | both | ALB `TargetResponseTime` (p90, seconds) | p90 above 6 s for two 5-minute periods | OK (no requests) | More searches than semantic capacity: requests wait up to 5 s and are answered by keyword search; or the database is slow | Check `kb-cpu` and RDS CPU. If CPU is high and the group is not at 4, scaling is on its way (10 minutes per instance under load); for class, raise capacity ahead with `infra/class-capacity.sh up`. |
| `kb-cpu` | both | EC2 `CPUUtilization`, average over the group | Above 85 percent for 15 minutes | INSUFFICIENT_DATA | Sustained search load (models are CPU bound) | In postgres mode the target tracking policy (50 percent) should be adding instances; if `kb-capacity-max` is also in ALARM, the group is at its limit. In sqlite mode there is only one instance: reduce load or switch to postgres mode. |
| `kb-capacity-short` | both | Group desired minus in-service (metric math, 1 minute) | At least 1 instance missing for 15 consecutive minutes | INSUFFICIENT_DATA | New instances fail to launch (sandbox limits, AMI or profile), fail to boot (image download, database import), or fail health checks | `aws autoscaling describe-scaling-activities` for the error; `s3://<bucket>/debug/` for boot logs. Never start instances by hand (the sandbox suspends the account past 9). |
| `kb-capacity-max` | postgres | Group `GroupInServiceInstances` (max) | 4 instances in service | OK | Scaled out to the maximum (expected during a load test or class) | Nothing if expected. If not, look for what drives CPU. The maximum stays 4. |
| `kb-db-cpu` | postgres | RDS `CPUUtilization` (avg) | Above 80 percent for 15 minutes | INSUFFICIENT_DATA | Heavy search or import load; db.t3.micro is burstable and runs out of CPU credits | Check `CPUCreditBalance` in the RDS console. Run3 at 50 users peaked at 17 percent, so this means something unusual (a bulk import, a runaway query). |
| `kb-db-connections` | postgres | RDS `DatabaseConnections` (max) | More than 60 for 10 minutes | INSUFFICIENT_DATA | The app uses at most 6 per instance (5 pooled plus the health monitor), 24 at 4 instances; run3 peaked at 18. More means leaked connections or another client | The limit for db.t3.micro is roughly 80 to 110. Replacing instances (rolling) closes their pools. |
| `kb-db-storage` | postgres | RDS `FreeStorageSpace` (min) | Less than 2 GB free (of 20) for 10 minutes | INSUFFICIENT_DATA | Unexpected growth: the corpus itself is tens of MB | Find the large tables; storage cannot be raised past the lab's limit (200 GB requests are denied, 40 GB allowed). |
| `kb-keyword-mode` | `MetricsMode` api or emf | `KbMcp` `KeywordSearches` (sum, Mode=keyword) | At least 1 keyword answer in a minute | OK | Models failed or timed out, the instance was busy (admission control), or the fallback toggle is on | Check `/healthz` `models` and `fallback_toggle`. Fires on purpose in demo step 7 when it exists. |
| `kb-high-latency` | `MetricsMode` api or emf | `KbMcp` `LatencyMs` (p90, Mode=semantic) | Above 2000 ms over 5 minutes | OK | Slow semantic search | As `kb-response-time`. |
| `kb-memory` | `MetricsMode` api or emf | `CWAgent` `mem_used_percent` (avg over the group) | Above 85 percent for 10 minutes | OK | Model memory plus load | Restart or replace the instance; check for a leak in the logs. |

"Missing data: OK" (`notBreaching`) is used where no data means no traffic, which is fine. "INSUFFICIENT_DATA" (`missing`) is used for metrics AWS publishes continuously, so silence shows up as a state of its own rather than passing as OK.

The target tracking policy creates two more alarms of its own (`TargetTracking-...-AlarmHigh` and `AlarmLow`). They drive scaling, have no SNS action, and should not be edited.

## Dashboards

- `kb-mcp`: requests and 5xx (target and load balancer), target response time p50 and p90, healthy and unhealthy hosts, desired and in-service instances with average CPU. AWS metrics only, so it has data with the sandbox role.
- `kb-mcp-db` (postgres mode): RDS CPU, connections, free storage.
- `kb-mcp-search` (`MetricsMode` api or emf): search latency and count by mode, memory.

## Notifications

`kb-alerts` has one email subscription, `KB_ALERT_EMAIL` from `~/.config/kb-mcp/up.env`. AWS sends a confirmation email on every new topic (a new account or a recreated `kb-app`); alarms reach the inbox only after the link in it is clicked (**HUMAN**). Check with `aws sns list-subscriptions-by-topic --topic-arn <AlertTopicArn>`: a confirmed subscription has a full ARN, an unconfirmed one shows `PendingConfirmation`.

To prove delivery without waiting for a failure, set one alarm's state by hand. It changes nothing but the alarm state, and the next evaluation (within a minute or two) puts the real state back:

```bash
aws cloudwatch set-alarm-state --alarm-name kb-5xx --state-value ALARM --state-reason "notification test"
```

Expect an ALARM email, then an OK email when CloudWatch re-evaluates.

## How to check that an alarm gets data

```bash
aws cloudwatch describe-alarms --alarm-name-prefix kb- \
  --query 'MetricAlarms[].[AlarmName,StateValue,StateReason]' --output table
aws cloudwatch list-metrics --namespace AWS/ApplicationELB --metric-name TargetResponseTime
aws cloudwatch get-metric-statistics --namespace AWS/AutoScaling --metric-name GroupDesiredCapacity \
  --dimensions Name=AutoScalingGroupName,Value=<AsgName> --statistics Maximum --period 60 \
  --start-time "$(date -u -d '-30 min' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)"
```

An alarm whose `StateReason` keeps saying "Insufficient Data" on a running stack reads a metric that does not exist under those exact dimensions.

## Status

| Item | State |
| --- | --- |
| Alarm and dashboard definitions | Implemented; `tests/test_infra.py` checks the alarm set per mode, thresholds, dimensions, that no alarm with the default `MetricsMode` reads a custom namespace, and that the RDS alarms read the `kb-DbInstanceId` export; cfn-lint and `validate-template` pass |
| Deployed | **Yes, 2026-11-13** (account `983926826929`, `postgres` mode, `MetricsMode=off`; torn down the same day). 11 alarms created, the set this table lists for postgres mode without the `MetricsMode` ones |
| Each alarm's metric has data | **Verified 2026-11-13** for the alarms whose state shows missing data: `kb-cpu`, `kb-capacity-short`, `kb-db-cpu`, `kb-db-connections` and `kb-db-storage` were OK (not INSUFFICIENT_DATA), and `kb-no-healthy-host` went from ALARM to OK when `HealthyHostCount` arrived. The other five (`kb-unhealthy-host`, `kb-5xx`, `kb-elb-5xx`, `kb-response-time`, `kb-capacity-max`) treat missing data as OK, so their OK state does not prove data; their metrics are the ALB and group metrics that had data in phases 23 and 23a and that `tests/test_infra.py` checks against the documented names and dimensions (phase 26, T5) |
| `kb-no-healthy-host` during a deploy | **Expected, not a fault.** While `kb-app` is created no target is healthy yet and missing data is breaching, so the alarm goes to ALARM about 3 minutes in and clears once the first target is healthy: on 2026-11-13 ALARM at 17:20:13 UTC, OK at 17:27:13 UTC (7 minutes), the same pattern as every deploy since October (alarm history). Once the SNS subscription is confirmed, expect one ALARM email and one OK email per new `kb-app` (a new topic needs a new confirmation, so on a first deploy the emails may not arrive). If it stays in ALARM more than about 15 minutes after the stack finishes, follow its row above |
| SNS email delivery | **Subscription verified 2026-11-13:** the confirmation email for the new topic arrived and the user confirmed it. The alarm emails from the `kb-no-healthy-host` change were not recorded in that test. Earlier: `docs/PROGRESS.md` deviation 8 records that the ALB alarms and SNS emails worked |
