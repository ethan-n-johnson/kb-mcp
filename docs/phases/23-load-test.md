# Phase 23: Load test and scaling evidence

**Days:** Sat Nov 7 to Sun Nov 8 · **Depends on:** 22 · **Where:** laptop + AWS · **PLAN.md sections:** Course alignment (Elasticity, Performance efficiency)

## Goal

Show the Auto Scaling group scale out under load and back in afterwards, with numbers and screenshots for the final presentation.

## Read first

- `docs/PROGRESS.md` (phase 21 and 22 latencies), `infra/app.yaml` (scaling policy, `SearchRate`)

## Tasks

1. `loadtest/locustfile.py` (Locust, dev dependency only): one user class that searches a fixed list of 20 questions (the eval questions plus paraphrases) through `GET /api/search`, with 1 to 3 s waits, and a light 1-in-20 `GET /healthz`. No writes.
2. Raise the rate limit for the test only: `KB_SEARCH_RATE=6000/minute` in `up.env`, redeploy. Every request comes from the laptop's one address, so the default 30 per minute would answer 429. Put it back afterwards and redeploy.
3. **HUMAN:** run the test from the laptop through CloudFront:
   `locust -f loadtest/locustfile.py --host "$CF" --headless -u 30 -r 2 -t 25m --csv loadtest/results/run1`.
   Watch the EC2 console Auto Scaling group Activity tab. Start the test at least 30 minutes before the lab session ends.
4. Record in PROGRESS.md: users, requests per second, p50, p95 and max latency, failure rate (by status), the time from load start to the first scale-out activity, to the new instance in service, the peak instance count, and the time to scale back in after the test.
5. Screenshots for the report (save them in `~/Pictures/Screenshots/kb_screenshots/`): the Auto Scaling Activity history, the CloudWatch graph of `CPUUtilization` (group average) with `GroupInServiceInstances` on the same graph, the Locust summary, and the `kb-capacity-max` email if the group reached 4.
6. If the group never scales out (CPU stays under 50%), increase users in steps of 10 up to 60. If p95 goes above 10 s before scaling helps, that is a finding: record it with the warmup time as the likely cause (10 minutes to load the image and models).
7. Commit the locustfile and the CSV summaries (not the raw stats history if it is large).

## Do not

- Load the ALB address directly; go through CloudFront like real clients.
- Leave the raised rate limit deployed.

## Acceptance

- [ ] At least one scale-out and one scale-in recorded with timings.
- [ ] Latency and failure numbers recorded; screenshots saved.
- [ ] Rate limit back to `30/minute` and redeployed.
- [ ] Phase log updated; commit `phase 23: load test and scaling evidence`.
