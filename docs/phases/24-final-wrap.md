# Phase 24: Final README, extension test and submission

**Days:** Mon Nov 9 to Wed Nov 11 (Thu Nov 12 is buffer; due Fri Nov 13) · **Depends on:** 23 · **Where:** local + AWS · **PLAN.md sections:** Course alignment; Demo corpus and demo script

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
5. **Final presentation** (the midpoint deck is the starting point): replace "planned" items with results, add the phase 2 screenshots (two AZs, rolling deploy, scaling graph, Locust summary), and a short lessons-learned slide.
6. **HUMAN:** one full rehearsal against the live stack, then record a backup video.
7. After the final presentation: revoke the demo write token (remove `demo:` from `s3://<bucket>/config/write-tokens` and the local file), run `infra/up.sh save`, then delete `kb-app` and `kb-db` if the class is done. Never delete `kb-network` or `kb-data` without asking.

## Acceptance

- [ ] `KB_METRICS_MODE=off` deployed; the 30 s warning is gone from the container log.
- [ ] Extension tested with screenshots, or marked untested everywhere it appears.
- [ ] README covers both phases with diagrams, results and dated cost notes.
- [ ] `scripts/demo_check.sh "$CF"` prints all PASS against the live stack.
- [ ] Repo on GitHub matches the laptop; CI green; final deck submitted.
- [ ] Phase log updated; commit `phase 24: final README and submission`.
