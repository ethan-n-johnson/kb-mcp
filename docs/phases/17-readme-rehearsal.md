# Phase 17: README and rehearsal

**Day:** 7 (Tue Oct 13) · **Depends on:** 16 · **Where:** local + AWS · **PLAN.md sections:** Course alignment; Demo corpus and demo script; Risks, cut lines and open items

## Goal

A README a grader can follow, a demo runbook with exact commands, an automated pre-demo check, and two full rehearsals including failover.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (all of it: outputs, deviations, timings, open issues), `docs/SECURITY_REVIEW.md`
- `docs/PLAN.md`: "Course alignment", "Demo corpus and demo script"

## Tasks

1. `README.md`:
   - One-paragraph overview and what the demo shows.
   - Two Mermaid diagrams: the application (interfaces into one service layer, models, index, SQLite) and the AWS deployment (VPC, 2 AZs, public and private subnets, CloudFront, ALB, ASG, instance root volume with the database, S3 (images, snapshots, backups, originals) through the gateway endpoint, NAT, CloudWatch, SNS). Say why there is no EFS and no ECR (sandbox AccessDenied) and show the snapshot and restore arrows.
   - The three stacks and what each holds; deploy steps and teardown steps exactly as the scripts in `infra/` run them.
   - Course alignment and Well-Architected tables from PLAN.md, **updated** with what was actually built and the deviations from PROGRESS.md.
   - Cost notes: which resources bill hourly (NAT gateway, ALB, EC2 instance) versus almost nothing idle (S3 with the image, snapshots and backups at this size; CloudFront within free tier; the EBS root volume bills while the instance exists). Look up current us-east-1 prices on the AWS pricing pages and state the date you checked; do not quote prices from memory.
   - Scale-out path: RDS PostgreSQL with pgvector, stateless app instances, ASG max above 1, custom domain with ACM on the ALB, MCP OAuth 2.1.
   - Known limitations (copy from the security review).
   - If the instructor asked for extra deliverables in phase 00 (Pricing Calculator estimate, written Well-Architected review), add them here or as separate files and link them.
2. `docs/DEMO.md`: the 10-step demo script from PLAN.md rewritten as a runbook: for each step the exact URL, query, prompt or command, what the audience should see, the expected `mode` and `confidence`, and the fallback if it fails. Include a pre-demo checklist (start the AWS Academy session 30+ minutes early, deploy `kb-app` if deleted, re-run `claude mcp add` if the CloudFront domain changed, set the extension's server URL, warm the models with one search, turn the fallback toggle off, open the needed browser tabs and terminals) and the failover timing from phase 12 so the talk track fills the gap.
3. `scripts/demo_check.sh <base-url>`: hits `/healthz` (expect `semantic`, models `ok`), the two demo searches (expect `high`), an out-of-corpus search (expect `low`), `/api/stale` (expect the 3 planted docs), `/api/gaps`, MCP `tools/list` (11 tools), and the static asset cache; prints PASS or FAIL per line and exits non-zero on any FAIL. It must not write anything.
4. Run `scripts/demo_check.sh` against the CloudFront URL; fix anything that fails (inside the scope of earlier phases; record what changed).
5. **HUMAN:** rehearse the full demo twice with `docs/DEMO.md`, including failover. Note timing per step and anything awkward; Sonnet updates `DEMO.md` from the notes.
6. **HUMAN:** record a backup video of one clean run.
7. Add a post-demo section to `DEMO.md`: revoke the demo token (edit `/kb/write-tokens` to remove `demo:`), delete `kb-app` if the class is done, and how to bring it back.

## Do not

- Add features. Only fixes that make the demo check pass.

## Acceptance

- [ ] `README.md` and `docs/DEMO.md` exist and render (Mermaid diagrams included).
- [ ] `scripts/demo_check.sh https://<cloudfront>` exits 0.
- [ ] The user confirms two rehearsals done and the backup video recorded.
- [ ] Phase log updated; commit `phase 17: README and rehearsal`.
