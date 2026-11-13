# Phase 24: Classroom readiness, README and extension test

First planned as the final phase ("Final README, extension test and submission"); phases 25 to 27 followed, and the final wrap-up is phase 27 (`27-final-wrap-up.md`).

**Days:** Wed Nov 11 to Thu Nov 12 (due Fri Nov 13) · **Depends on:** 23 · **Where:** local + AWS · **PLAN.md sections:** Course alignment; Demo corpus and demo script

## Goal

Finish what phase 17 left open, close the small loose ends from phase 1, test the Chrome extension, and hand in a repo, README and final presentation that describe the phase 2 system as built.

## Read first

- `docs/phases/17-readme-rehearsal.md` (its tasks still apply; this phase updates them for phase 2)
- `docs/PROGRESS.md` (everything recorded in phases 19 to 23), `docs/SECURITY_REVIEW.md`

## Tasks

1. **Loose ends from phase 1.**
   - `KB_METRICS_MODE=off`: no metrics sender at all (the sandbox role cannot publish; today it warns every 30 s). Make `off` the default in `app.yaml`.
   - PLAN.md demo step 8: the new instance restores from S3 or reads RDS; it never "mounts EFS".
2. **Chrome extension (HUMAN for the browser steps).** Load `extension/` unpacked, record the ID in PROGRESS.md, redeploy with `KB_EXTENSION_ORIGIN=chrome-extension://<id>`, set the CloudFront URL and a write token in its settings, run Test, capture one page, find it with search. Screenshot the popup result and the search hit. If any of it fails and the fix is not small, leave the extension as "written, not tested" in the README and the slides; do not let it eat the buffer day. (Cut line 1.)
3. **README.md**, following phase 17 task 1, updated for phase 2:
   - Two architecture diagrams: phase 1 (one instance, SQLite with S3 snapshots) and phase 2 (2 to 4 stateless instances, RDS PostgreSQL with pgvector, Multi-AZ or single-AZ as recorded).
   - Four stacks and what each holds; deploy, save and teardown commands as the scripts run them.
   - Course alignment and Well-Architected tables updated with what was built and every deviation.
   - Results: eval numbers (SQLite and PostgreSQL), search latency, failover timings (phase 12 and phase 22), load test numbers and the scaling timeline from phase 23.
   - Cost notes with current us-east-1 prices looked up on the AWS pricing pages and the date checked: NAT gateway, ALB, EC2 instances, RDS instance and storage (Multi-AZ doubles the instance cost), S3, CloudFront.
   - Known limitations (from the security review, including the per-instance rate limit and open MCP writes).
4. `docs/DEMO.md` and `scripts/demo_check.sh` from phase 17 tasks 2 and 3, updated: the check also confirms 2 healthy targets and `postgres` mode. (Cut line 2: if time is short, keep `demo_check.sh` and a one-page DEMO.md.)
5. **Classroom readiness.** The end goal is that the whole class uses the live stack at the same time, mostly from one campus network. What phase 23a showed (docs/PROGRESS.md, row 23a and its open issues): no failures and no killed instances at 30 Locust users, but about 0.7 semantic searches/s per instance, so the extra searches are answered by keyword search after waiting the 5 s `KB_SEARCH_TIMEOUT`, and scale-out takes about 10 minutes to add an instance.
   - **Rate limit.** `SearchRate` is 30/minute per client address per instance, and students behind one NAT share one address: 40 students at 1 to 2 searches a minute would get 429s on 2 instances. Admission control now protects the instances, so the limit only keeps one client from taking every semantic slot (and from scaling the group to 4 and burning credit). Raise the default to `120/minute` (the `SearchRate` default in `infra/app.yaml` and `KB_SEARCH_RATE` in `kb/config.py`, with their tests), and update the CLAUDE.md config table and `docs/SECURITY_REVIEW.md` (the rate limit row, limitations 12 and 23): admission control is now the protection against overload, and the limit only shares capacity fairly. Do not use `6000/minute` outside a load test.
   - **Classroom load test (AWS; HUMAN runs Locust).** Same Locust file, `-u 50 -r 2 -t 15m --csv loadtest/results/run3`, with the new default rate limit and a curl mode sampler running alongside. Record failures, `/healthz` p99, search p50 and p95, the semantic and keyword share, peak instances, and RDS `DatabaseConnections` and CPU (keyword search and the database pool were never pushed past 30 users). Pass: failures under 1%, no "ELB system health check failure" terminations, `/healthz` p99 under 1 s. If keyword search or RDS becomes the limit, record it under Open issues and in the README limitations; do not start a redesign this phase.
   - **Scale before class, not during it.** A class session is too short to wait 10 minutes for scale-out. Pick and document one way to have 4 healthy instances when class starts (for example a scheduled Auto Scaling action, or a temporary higher minimum through the stack, never above 4) and how capacity returns to 2 afterwards. Add it to `docs/DEMO.md` with the time to start it.
   - **Open MCP writes.** Anyone with the CloudFront URL can add, edit or delete documents through `/mcp` (README security note, PROGRESS deviation "MCP tools need no token"). With the whole class connected, decide with the user before sharing the URL: accept it (recovery is the versioned S3 export and `import-sqlite`; run `infra/up.sh save` right before class), or require the token for MCP writes. Record the decision in PROGRESS.md and in the README limitations.
   - **Student instructions.** One short section in the README (or `docs/STUDENTS.md`) with the CloudFront URL, how to search in the web UI, how to add the MCP server to Claude Code, what "keyword" mode in a result means, and that searches can take up to about 5 s when the class is busy.
6. **Final presentation** (the midpoint deck is the starting point): replace "planned" items with results, add the phase 2 screenshots (two AZs, rolling deploy, scaling graph, Locust summary), and a short lessons-learned slide.
7. **HUMAN:** one full rehearsal against the live stack, then record a backup video.
8. After the final presentation: revoke the demo write token (remove `demo:` from `s3://<bucket>/config/write-tokens` and the local file), run `infra/up.sh save`, then delete `kb-app` and `kb-db` if the class is done. Never delete `kb-network` or `kb-data` without asking.

## Acceptance

- [x] `KB_METRICS_MODE=off` deployed (2026-11-07 and 2026-11-13). The container log is not readable from the laptop (no CloudWatch Logs or SSM), so the missing warning is shown by the code and its test, not by the log.
- [x] Extension tested in Chrome on 2026-11-13 (capture stored and found from Claude Code); screenshots for the slides are HUMAN.
- [x] README covers both phases with diagrams, results and dated cost notes.
- [x] `scripts/demo_check.sh "$CF"` prints all PASS against the live stack (2026-11-07 and 2026-11-13).
- [x] `SearchRate` default is `120/minute` in the stack, config, tests, CLAUDE.md and SECURITY_REVIEW.md.
- [x] Classroom load test (run3, 50 users) recorded in PROGRESS.md: failures under 1%, no instance killed by the health check, semantic and keyword share, RDS connections and CPU.
- [x] Pre-class scaling method written in `docs/DEMO.md` and tried once (4 healthy targets before load, back to 2 after).
- [x] MCP write decision recorded; student instructions published.
- [ ] Repo on GitHub matches the laptop; CI green; final deck submitted. (GitHub and CI done 2026-11-13: `master` at `f53c9cf` green with a clean tree before the live test; the deck is HUMAN.)
- [x] Phase log updated; committed as `c0a75a8` "phase 24: classroom readiness, README and run3 (HUMAN steps pending)" (PR #7).
