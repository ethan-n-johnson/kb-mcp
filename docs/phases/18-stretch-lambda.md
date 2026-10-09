# Phase 18 (stretch): Weekly report Lambda

**Day:** only if phases 00 to 17 are done · **Depends on:** 17 · **Where:** AWS · **PLAN.md sections:** Course alignment (Decoupled and serverless architectures)

**Cut line 1:** this is the first thing to cut.

## Goal

An EventBridge schedule triggers a Lambda function weekly; it reads the public `stale` and `gaps` endpoints and emails a summary through the existing SNS topic. Shows a decoupled, serverless piece next to the EC2 app.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (CloudFront domain, SNS topic)

## Tasks

1. `infra/reports.yaml`, a separate small stack `kb-reports` (so it can be deleted without touching `kb-app`):
   - Parameters: `BaseUrl` (the CloudFront URL), `TopicArn` (from `kb-app`; add it as an export there if missing).
   - `AWS::Lambda::Function`, Python 3.12, inline code (`ZipFile`), role `arn:aws:iam::${AWS::AccountId}:role/LabRole`, timeout 30 s, env `BASE_URL`, `TOPIC_ARN`. Not in the VPC (it calls the public CloudFront URL).
   - Code: `urllib.request` GET `BASE_URL/api/stale?days=90&limit=20` and `BASE_URL/api/gaps?limit=10`; format a plain-text email (counts, then one line per item with a link to `BASE_URL/docs/<id>`); `sns.publish` with subject "KB weekly report".
   - `AWS::Scheduler::Schedule` (or an `AWS::Events::Rule` if EventBridge Scheduler is blocked) with `cron(0 14 ? * MON *)` (Monday 9 AM Central during daylight time), plus the `AWS::Lambda::Permission` the rule needs.
2. Add `deploy.sh reports`.
3. Deploy, then invoke once by hand: `aws lambda invoke --function-name <name> /dev/stdout`.

## Do not

- Create IAM roles; use `LabRole`. If the sandbox blocks Lambda or EventBridge, record it and stop.

## Acceptance

- [ ] **HUMAN:** the manual invoke produced a "KB weekly report" email listing the 3 stale docs and current gaps.
- [ ] The schedule exists and shows the next run time.
- [ ] README updated with a line on the reports stack; phase log updated; commit `phase 18: weekly report Lambda`.
