# Demo runbook

The 10-step demo from `docs/PLAN.md`, written for the phase 2 system (RDS PostgreSQL, 2 to 4 app instances) and for a class that uses the live stack at the same time. Cells marked `TBD (HUMAN)` are filled in after the rehearsal.

CloudFront URL of the current stack: `https://dsrig13j20ooy.cloudfront.net` (it changes when the sandbox account or `kb-app` is recreated; `CF=https://$(aws cloudformation describe-stacks --stack-name kb-app --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" --output text)`).

Students use `docs/STUDENTS.md`. The write token (`demo:` in `s3://<bucket>/config/write-tokens`, local copy `~/.config/kb-mcp/write-tokens`) is for the instructor only.

---

## Pre-class checklist

Work back from the start of class (T).

| When | What |
| --- | --- |
| T minus 90 min | Start the AWS Academy lab session (credentials last about 4 hours, a timed-out session can come back as a new empty account). Paste fresh credentials into `~/.aws/credentials`, check `aws sts get-caller-identity`. |
| T minus 75 min | `infra/up.sh`. It deploys what is missing: with every stack gone this took 26 minutes on 2026-11-07 (RDS Multi-AZ about 15 of them); with only `kb-app` gone about 15 minutes. Run it in a terminal you can leave. It restores the database from `~/.config/kb-mcp/kb-latest.db.gz` when the bucket has none. |
| T minus 40 min | `infra/class-capacity.sh up` (or schedule it, see below). The two extra instances start in parallel and each loads the image and the models: 2 min 50 s in the idle trial of 2026-11-07, about 10 minutes in phase 23 when instances booted under load. The lead time is a safety margin. |
| T minus 25 min | `scripts/demo_check.sh "$CF"` must print all PASS (mode `semantic`, both models ok, `postgres` mode, 2 or more healthy targets, the two demo searches `high`, the out-of-corpus search `low`, the 3 stale docs, 12 tools, MCP writes refused without a token, a static asset with `Cache-Control`). Note: the check's out-of-corpus search is logged as a gap whose text starts with "demo check:". |
| T minus 20 min | `infra/class-capacity.sh status` shows 4 in service and 4 healthy targets. |
| T minus 15 min | One warm-up search in the web UI and one in Claude Code (the first semantic search after a deploy is the slow one). Fallback toggle off (Maintenance page, or `demo_check.sh` says so). Re-run `claude mcp add` if the CloudFront domain changed (read-only for students, with `--header "Authorization: Bearer $KB_TOKEN"` for you). Set the extension's server URL (only if the extension was tested). Open the browser tabs (UI, CloudFormation, EC2 Auto Scaling, CloudWatch) and a terminal. |
| T minus 5 min | `infra/up.sh save` so the restore point matches the class start: the bucket has a fresh export and `~/.config/kb-mcp/kb-latest.db.gz` has a copy. It keeps the old laptop file in `saved/` and refuses a damaged or smaller download (phase 26). Share `docs/STUDENTS.md` and the URL. |

### Scaling to 4 instances before class

Waiting for CPU target tracking is too slow: the first scale-out alarm needs minutes of load (3 to 12 minutes in phases 23 and 23a) and a new instance then needs to load the image and models (about 10 minutes under load in phase 23). So raise the group's minimum before class.

```bash
infra/class-capacity.sh up                  # MinSize 4, DesiredCapacity 4 now (MaxSize stays 4)
infra/class-capacity.sh status              # sizes, in service count, healthy targets, scheduled actions
infra/class-capacity.sh down                # MinSize 2, DesiredCapacity 2 after class
# or hands off, times in UTC (the class runs 14:00 to 15:30 UTC on 2026-11-12, for example):
infra/class-capacity.sh schedule 2026-11-12T14:00:00Z 2026-11-12T15:30:00Z
```

- **Start at least 20 minutes before class.** `up` has to start now; `schedule START END` creates `kb-class-up` at START minus 20 minutes and `kb-class-down` at END.
- Only in `postgres` mode, never above 4 instances. The script refuses when `kb-app` is in `sqlite` mode or the group's MaxSize is not 4.
- **A stack update resets the size to the template's 2.** Do not run `infra/up.sh` or `infra/deploy.sh app` while scaled up; if you must, run `up` again afterwards.
- Afterwards `down` sets the minimum back to 2 and CPU target tracking scales in by itself.
- Four instances cost about $0.29 per hour against about $0.20 for two (README, cost notes).
- Trial run (2026-11-07, `kb-app` idle, 2 instances at the start): `up` at 18:51:26 UTC, the two extra instances launched at 18:51:43 and 4 healthy targets at 18:54:16, **2 min 50 s** after the command. `down` at 18:54:43: 2 instances in service after 16 s and the target group back to the 2 newer instances after 46 s (the older two were terminated and had their graceful stop; the ALB showed no unhealthy host). `/healthz` and the demo check passed with 4 targets and again afterwards.

---

## What students will see under load

The instance answers at most 2 semantic searches at a time. About 0.7 semantic searches per second per instance fit, so with 40 students searching at once on 4 instances, many searches wait for a slot, up to 5 s (`KB_SEARCH_TIMEOUT`), and are then answered by keyword search. They see a result with `mode: keyword` (a "keyword" badge in the UI) and a lower-quality ranking, never an error. A search can take up to about 5 s. A 429 means one address used its 120 searches per minute (per instance); one campus NAT shares one budget. In the load test (30 users, run2) there were no failures and no instance was killed. Run3 (50 users) is a human step below.

---

## Runbook per step

Times are rough. Keep a terminal with `aws` and `curl` ready for steps 7 and 8.

| # | Step | What to do | Expected | Fallback if it fails |
| --- | --- | --- | --- | --- |
| 1 | Architecture | Show the two Mermaid diagrams from `README.md` (phase 1 and phase 2), then the four stacks in the CloudFormation console (`kb-network`, `kb-data`, `kb-db`, `kb-app`). | Auto Scaling group with 2 (or 4) instances in two AZs, RDS Multi-AZ. | Slides only. |
| 2 | Web UI search | Open `$CF/`, search "how does an Auto Scaling group replace an unhealthy instance". | `mode: semantic`, `confidence: high`, top hit heading path on an Auto Scaling page, about 2.5 s. | If it says `keyword`, the toggle is on or the class is busy: turn the toggle off in Maintenance, search again. |
| 3 | Claude Code reads | `claude mcp add --transport http kb "$CF/mcp"`, then ask "Using the kb, how do I mount EFS on Amazon Linux?". | Claude calls `search`; `confidence: high`; answer from the EFS mount helper page. | `curl "$CF/api/search?q=how+do+I+mount+EFS+on+Amazon+Linux"` and read the result aloud. |
| 4 | Writes | With your token header: ask Claude to save a short runbook note (`upload`). Then "Save ~/Downloads/<file>.pdf to the kb": `upload_file`, approve the `curl`, then search for text inside the PDF. Use a PDF that is not stored yet (the SAA-C03 guide is already stored). Then upload `seeds/planted/fastapi-near-duplicate.md` and show `possible_duplicate`, then `merge`. Also show a student's read-only client being refused: "This tool needs a write token". | `created`, `upload_link` then 201, `possible_duplicate` with candidate similarity about 0.9989, merge result; the refusal sentence for the tokenless client. | Use the web UI Add page (needs the token in the token box) or `curl -X POST $CF/api/docs/file -H "Authorization: Bearer $KB_TOKEN" -F file=@...`. |
| 5 | Chrome extension | Capture a page with the extension, then find it with search. **Only if the extension test passed** (see below). | The page is stored and found. | Skip the step and say the extension is written and not yet tested in Chrome. |
| 6 | Outside the corpus | Ask "how do I bake a sourdough loaf" (any out-of-corpus question). Then open Maintenance or `$CF/api/gaps`. | `confidence: low`, "no good answer" from Claude; the question in the gaps list. | Show `gaps` anyway (the `demo_check.sh` question is in it). |
| 7 | Keyword fallback | Maintenance page: switch the fallback toggle on, repeat a search, then switch it off. In `postgres` mode the toggle is stored in the database, so every instance obeys it within about 10 s. The `kb-keyword-mode` alarm does not exist with the default `MetricsMode=off` (no custom metrics in the sandbox, phase 25.6); alarm emails come from the load balancer instead (step 8). | `mode: keyword` still answering, with a snippet. Toggle off: back to `semantic`. | Say it and show `mode` in the JSON. |
| 8 | Failover | Pick an instance and mark it unhealthy: `aws autoscaling set-instance-health --instance-id <id> --health-status Unhealthy` (or terminate it in the EC2 console). Search repeatedly meanwhile. The ALB stops sending to it, the group launches a replacement in either AZ, and the other instances keep answering. `kb-unhealthy-host` emails after 2 minutes. Start this early and talk through steps 9 and 10. | Searches keep working. Replacement healthy about 2 min 40 s later (phase 22: 53 of 54 searches returned 200 meanwhile; 1 got no response). In `sqlite` mode the replacement restores the S3 snapshot instead and the service is down meanwhile. Live rehearsal timing: TBD (HUMAN). | Show the earlier screenshots of the Auto Scaling activity history. |
| 9 | Stale and verify | Maintenance page or `stale` in Claude Code: 3 planted docs. `verify` one (needs the token). | 3 docs, 200 days old; after `verify` it leaves the list. | `curl "$CF/api/stale"`. |
| 10 | Close | Well-Architected table (README), load-test results (run2: 0 failures, run3: 0 failures in 6493 requests at 50 users on 4 instances), scale-out path, known limitations. | | |

### Rehearsal notes (HUMAN)

Fill in after each rehearsal: time per step, what was awkward, any step that fell back.

| Rehearsal | Date | Steps timed | Failover seen by audience | Notes |
| --- | --- | --- | --- | --- |
| 1 | TBD (HUMAN) | TBD (HUMAN) | TBD (HUMAN) | |
| 2 | TBD (HUMAN) | TBD (HUMAN) | TBD (HUMAN) | |

Failover timing to fill the talk track: marked unhealthy to replacement healthy, about 2 min 40 s (phase 22, 2 instances); a rolling redeploy of both instances, about 5 min 19 s. TBD (HUMAN): the timing seen in the rehearsal.

---

## Post-class

In this order:

1. `infra/class-capacity.sh down` (and delete the two scheduled actions if you used `schedule`:
   `aws autoscaling delete-scheduled-action --auto-scaling-group-name <AsgName> --scheduled-action-name kb-class-up`, then `kb-class-down`). Check `infra/class-capacity.sh status`.
2. Revoke the demo write token: remove the `demo:` entry from `s3://<bucket>/config/write-tokens` and from `~/.config/kb-mcp/write-tokens`. Tokens are cached until the container restarts (SECURITY_REVIEW limitation 10), so restart the instances too (`aws autoscaling start-instance-refresh` or just redeploy). Never print the file; edit it with an editor.
3. `infra/up.sh save`, and check the downloaded `~/.config/kb-mcp/kb-latest.db.gz` is newer than the class.
4. Only if the class is done: `infra/teardown-app.sh`, then, after the save worked, delete `kb-db` (README, "Deploy, save and tear down"). Never delete `kb-network` or `kb-data` without asking. Bring everything back with `infra/up.sh`.

---

## HUMAN: classroom load test (run3)

Run it from the laptop through CloudFront, with the new default rate limit (`120/minute`, so no `KB_SEARCH_RATE` override) and after `infra/class-capacity.sh down` if you want to see scale-out, or after `up` if you want to see steady state with 4 instances. Pass: failures under 1 percent, no "ELB system health check failure" terminations, `/healthz` p99 under 1 s. If keyword search or RDS becomes the limit, record it under Open issues and in the README limitations; do not redesign.

```bash
CF=https://<cloudfront domain>
# 1. Locust (same file as run1 and run2), 50 users, 15 minutes
locust -f loadtest/locustfile.py --host "$CF" --headless -u 50 -r 2 -t 15m \
    --csv loadtest/results/run3 2>&1 | tee loadtest/results/run3.log

# 2. In a second terminal at the same time: the mode sampler, 40 searches 3 s apart
for i in $(seq 1 40); do
  curl -s -m 30 -G --data-urlencode "q=how do I enable versioning on an S3 bucket" "$CF/api/search" \
    -o /tmp/sample.json -w '%{http_code} %{time_total}s %{errormsg}\n' | tr '\n' ' '
  jq -r '.mode // "no-mode"' /tmp/sample.json 2>/dev/null || echo "no-mode"
  sleep 3
done | tee loadtest/results/run3-modes.txt
# afterwards: sort loadtest/results/run3-modes.txt | awk '{print $NF}' | sort | uniq -c

# 3. Afterwards, with UTC start and end of the load as START and END
START=2026-11-12T14:00:00Z; END=2026-11-12T14:20:00Z
ASG=$(aws cloudformation describe-stacks --stack-name kb-app --query "Stacks[0].Outputs[?OutputKey=='AsgName'].OutputValue" --output text)
LB=$(aws cloudformation describe-stacks --stack-name kb-app --query "Stacks[0].Outputs[?OutputKey=='LoadBalancerFullName'].OutputValue" --output text)
TG=$(aws cloudformation describe-stacks --stack-name kb-app --query "Stacks[0].Outputs[?OutputKey=='TargetGroupFullName'].OutputValue" --output text)
DB=$(aws cloudformation describe-stack-resource --stack-name kb-db --logical-resource-id Db --query StackResourceDetail.PhysicalResourceId --output text)

# Scaling activities: look for "ELB system health check failure" (must be none)
aws autoscaling describe-scaling-activities --auto-scaling-group-name "$ASG" --max-items 30 \
  --query 'Activities[].[StartTime,StatusCode,Cause]' --output text | grep -i "health check failure" || echo "no health check terminations"

# ALB 5XX and unhealthy hosts, one-minute points
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_ELB_5XX_Count \
  --dimensions Name=LoadBalancer,Value="$LB" --start-time "$START" --end-time "$END" --period 60 --statistics Sum \
  --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Sum]' --output text
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name UnHealthyHostCount \
  --dimensions Name=LoadBalancer,Value="$LB" Name=TargetGroup,Value="$TG" --start-time "$START" --end-time "$END" --period 60 --statistics Maximum \
  --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Maximum]' --output text

# RDS connections and CPU
for M in DatabaseConnections CPUUtilization; do
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name $M \
    --dimensions Name=DBInstanceIdentifier,Value="$DB" --start-time "$START" --end-time "$END" --period 60 --statistics Maximum \
    --query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Maximum]' --output text
done

# Peak instances in service
aws cloudwatch get-metric-statistics --namespace AWS/AutoScaling --metric-name GroupInServiceInstances \
  --dimensions Name=AutoScalingGroupName,Value="$ASG" --start-time "$START" --end-time "$END" --period 60 --statistics Maximum \
  --query 'max(Datapoints[].Maximum)' --output text
```

Record in `docs/PROGRESS.md` (row 24): failures, `/healthz` p99, search p50 and p95, the semantic and keyword share from the sampler, peak instances, RDS `DatabaseConnections` and CPU. `loadtest/results/run2.log` was untracked; commit it with this phase together with the run3 files.

---

## HUMAN: Chrome extension test

Cut line 1: if any step fails and the fix is not small, leave the extension marked "written, not yet tested in Chrome" in the README and the slides; do not let it eat the buffer day.

1. Chrome: `chrome://extensions`, turn on Developer mode, **Load unpacked**, choose the `extension/` folder. Copy the extension ID (32 letters) and put it in `docs/PROGRESS.md` (Outputs table).
2. Redeploy so CORS allows it: add `KB_EXTENSION_ORIGIN=chrome-extension://<id>` to `~/.config/kb-mcp/up.env`, run `infra/up.sh`. (A stack update resets class capacity to 2; run `infra/class-capacity.sh up` again afterwards if needed.)
3. Open the extension's settings page: server URL `https://<cloudfront domain>` (the ALB URL is refused on purpose), and a write token (the part after the colon in `~/.config/kb-mcp/write-tokens`). Press **Test**: it should say connected.
4. Open an article page (for example an AWS documentation page you have not stored), open the popup, press **Capture**. Expect "created" with chunks. Try again: expect the duplicate notice. Select a paragraph and capture with "selection only".
5. Search for a sentence from that page in the web UI. Expect the new document in the results.
6. Take two screenshots: the popup result and the search hit. Add them to the slides and note the result in PROGRESS.md.
