# Phase 27: Final wrap-up

**Days:** after Fri Nov 13 · **Depends on:** 26 and the live test of 2026-11-13 · **Where:** local only (no AWS calls, no deploy) · **Replaces:** the planned phases 27 to 31 of `docs/FINAL_PROJECT_ROADMAP.md` (user decision 2026-11-13: no more phases except this one)

## Goal

The documents say only what was verified, the project is marked finished, and the user has what they need for the deck and the rehearsal.

## Read first

- This file (the live results below are the evidence; they are recorded nowhere else yet)
- `docs/PROGRESS.md` (phase log rows 24 to 26, "Open issues")
- `docs/FINAL_PROJECT_ROADMAP.md`, sections 6.5 and 11
- `README.md`, `docs/DEMO.md`, `docs/RECOVERY.md`, `docs/ALARMS.md`, `docs/SECURITY_REVIEW.md` (status sections only)

## Live test, 2026-11-13 (the evidence for this phase)

Run by Claude (steps 2 to 7) and the user (steps 3, 8 to 10). Account `983926826929` (the Cloud Architecting sandbox used in phases 19 to 24), `KB_DATABASE_MODE=postgres`.

| Check | Result |
| --- | --- |
| Starting state | No kb stacks; CI green on `master` (`f53c9cf`, merge of PR #10, phase 26); clean working tree |
| `infra/up.sh` | Exit 0, 16:57:47 to 17:25:08 UTC (27 min 20 s) for `kb-network`, `kb-data`, `kb-db` (RDS), image build and upload, `kb-app`. Image `kb-mcp:f53c9cf` (not dirty), 398 MB compressed, 1.24 GB unpacked. The laptop copy (65 docs, 790 chunks) was uploaded and imported into RDS. CloudFront `d545eegao70a2.cloudfront.net` |
| SNS | Subscription email confirmed by the user |
| `scripts/demo_check.sh` | All 14 lines PASS: `/healthz` 200 with 790 chunks, semantic mode, both models ok, fallback toggle off, postgres mode, 2 healthy targets, demo searches 1 and 2 high confidence, out-of-corpus search low confidence, `/api/stale` lists the 3 planted docs, `/api/gaps` 200, MCP `tools/list` 12 tools, MCP write without a token refused, static asset `cache-control: public, max-age=300` |
| `/livez` (phase 25.3) | `{"status":"alive"}` through CloudFront; the target group health check path is `/livez`; both targets healthy |
| Multi-AZ, scale-out settings | 2 instances, one in `us-east-1a`, one in `us-east-1b`; group min 2, max 4, desired 2 |
| Alarms (phase 25.6) | 11 alarms created. 10 OK from the start, including the new `kb-elb-5xx`, `kb-response-time`, `kb-cpu`, `kb-capacity-short`, `kb-db-cpu`, `kb-db-connections` and `kb-db-storage`. `kb-no-healthy-host` went to ALARM at 17:20:13 UTC (no datapoints for 3 periods while the stack was created; missing data is treated as breaching) and back to OK at 17:27:13 UTC, the same pattern as every deploy since October (alarm history). So all AWS-metric alarms received data |
| REST write tokens | `POST /api/docs` without a token 401, with a wrong token 401, with a write token 201 (doc 66, 1 chunk); search found it; `DELETE` without a token 401, with the token 200; chunks back to 790 |
| Claude Code over MCP (HUMAN) | Entry re-added in local scope with the new URL and a write token; reads and writes worked |
| Chrome extension (HUMAN) | Settings pointed at the new URL; a capture was saved to the knowledge base and then found from Claude Code. **The extension is tested** (it was "written, not yet tested in Chrome" since phase 14) |
| `infra/up.sh save` (phase 26 code, first live run) | Kept the previous 65-doc copy as `saved/kb-20261113T175011Z-before.db.gz`, checked the download and replaced the laptop copy: now 79 docs, 1089 chunks (`check_sqlite_copy.py`: integrity ok), dated copy `saved/kb-20261113T175011Z.db.gz` |
| Teardown | `infra/teardown-app.sh` and `infra/teardown-db.sh` worked. Afterwards only `kb-network` and `kb-data` remain; no RDS instance, no running instances, no NAT gateway, no Elastic IP. CloudFormation took the final RDS snapshot `kb-db-snapshot-db-uz70pqrr8z1u` (20 GB, 18:08:12 UTC): kept or deleted by the user's choice; it bills storage until deleted and is not needed (the laptop copy and the S3 export hold the same data) |

Not tested live (say so wherever they are mentioned): RDS point-in-time restore (`infra/restore-test-db.sh`), a forced RDS failover, scale-out under load in this phase (the phase 23 and 24 runs remain the scale-out evidence), the nightly export claim across instances, the SQLite (`sqlite` mode) deployment of phases 25 and 26.

## Tasks

1. `docs/PROGRESS.md`: a phase log row "live test 2026-11-13" with the table above in short form; the row for this phase; close the open issues the live test settled (the dead `kb` MCP entry, the extension test, the review finding about the earlier `kb-app`, which needs only a note).
2. `README.md`: the extension status (tested 2026-11-13); phases 25 and 26 in the status or results section as verified live on 2026-11-13 (with the list of what was not tested); the save behavior; the cost notes stay as they are unless they contradict the live run.
3. `docs/DEMO.md`, `docs/RECOVERY.md`, `docs/ALARMS.md`: status tables updated from "not deployed" to the live results where the table above covers them; the `kb-no-healthy-host` startup alarm and its 7 minute clear noted in `docs/ALARMS.md`.
4. `docs/FINAL_PROJECT_ROADMAP.md`: marked complete; section 11 checklist ticked or marked out of scope.
5. `docs/phases/24-final-wrap.md`: tick the acceptance items the live test settled (extension tested, demo check, CI green, GitHub matches); leave the deck and rehearsal to the user.
6. A short list for the user's deck: the verified numbers and where each is recorded (no slides are written in this phase).

## Out of scope

Any code, template or test change (record a defect under "Open issues" instead). Any AWS call. The deck itself, the rehearsal and the submission (HUMAN).

## Acceptance

- [ ] Every "not deployed", "untested" or "planned" claim in README, DEMO, RECOVERY, ALARMS and the roadmap either matches the live results above or says why it stays untested.
- [ ] No document claims a live result that is not in the table above or in an earlier PROGRESS row.
- [ ] `pytest -q` still passes (docs only, as a guard); no em dashes added.
- [ ] Phase log updated; commit `phase 27: final wrap-up`.
