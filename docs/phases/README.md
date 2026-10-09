# Phases

`docs/PLAN.md` split into 19 phases sized for one Claude Code session each with Sonnet. Each phase file is self-contained: goal, what to read, numbered tasks, scope fences, **HUMAN** steps, and acceptance checks that Sonnet must run before calling the phase done.

## Before the first phase

Set up your computer with [`docs/SETUP.md`](../SETUP.md) (Linux and Windows): AWS CLI, Docker, Python, the credentials file, and the steps to repeat every lab session.

## How to run a phase

```bash
cd ~/Documents/"Cloud Computing"/kb-mcp
claude --model sonnet
```

Then paste:

```text
Execute docs/phases/NN-name.md. Follow CLAUDE.md. Plan first, then implement,
then run every acceptance check and show me the results. Stop at any HUMAN step.
```

Tips:

- Use `/clear` (or a fresh session) between phases. `docs/PROGRESS.md` carries the state forward, so Sonnet does not need the old conversation.
- For the larger phases (05, 07, 08, 09), start in plan mode (Shift+Tab) and approve the plan before it writes code.
- Before any phase that touches AWS, start your AWS Academy session and paste fresh credentials into `~/.aws/credentials` (see "Every lab session" in `docs/SETUP.md`). They expire after a few hours.
- If a phase fails its acceptance checks twice in a row, stop and ask Opus to review the phase output against PLAN.md rather than letting Sonnet keep patching.

## Index

| # | Phase | Day | Where | Depends on | HUMAN steps | Cut line |
| --- | --- | --- | --- | --- | --- | --- |
| 00 | [Preflight and repo setup](00-preflight.md) | 1 Wed Oct 7 | local + AWS (read only) | none | AWS creds, name the environment | |
| 01 | [Network and data stacks](01-network-data-stacks.md) | 1 | AWS | 00 | create token parameter | |
| 02 | [App skeleton and schema](02-app-skeleton.md) | 1 | local | 00 | | |
| 03 | [Container image and S3 delivery](03-container-image.md) | 1 | local + AWS | 01, 02 | | |
| 04 | [Ingestion pipeline](04-ingestion.md) | 2 Thu Oct 8 | local | 02 | | |
| 05 | [Core write path](05-core-write-path.md) | 2 | local | 04 | | |
| 06 | [App stack](06-app-stack.md) | 3 Fri Oct 9 | AWS | 01, 03 | Session Manager check | |
| 07 | [Search pipeline](07-search.md) | 3 | local, then AWS | 05 | | 5th: reranker |
| 08 | [Remaining operations](08-remaining-operations.md) | 4 Sat Oct 10 | local | 07 | | |
| 09 | [MCP layer](09-mcp.md) | 4 | local, then AWS | 08, 06 | `claude mcp add`, try prompts | |
| 10 | [CloudFront edge](10-cloudfront.md) | 5 Sun Oct 11 | AWS | 09 | re-add MCP server | 4th: CloudFront |
| 11 | [Seed corpus and tuning](11-corpus-tuning.md) | 5 | local, then AWS | 09 | review test questions | |
| 12 | [Backup, restore, failover](12-backup-failover.md) | 5 | AWS | 11 | approve instance termination | |
| 13 | [Web UI](13-web-ui.md) | 6 Mon Oct 12 | local, then AWS | 08 | click through | 3rd: maintenance page |
| 14 | [Chrome extension](14-extension.md) | 6 | local | 13 | load unpacked, test capture | 2nd: selection-only |
| 15 | [Monitoring and alarms](15-monitoring.md) | 6 | AWS | 12 | confirm SNS email | |
| 16 | [Hardening](16-hardening.md) | 7 Tue Oct 13 | local, then AWS | 15 | | |
| 17 | [README and rehearsal](17-readme-rehearsal.md) | 7 | local + AWS | 16 | rehearse twice, record video | |
| 18 | [Stretch: weekly report Lambda](18-stretch-lambda.md) | if time | AWS | 17 | confirm email | 1st: cut first |

Day 1 is heavy (00 to 03). If it slips, 03 can move to the morning of Day 2; 06 needs it.

## Never cut

The three CloudFormation stacks, ingestion, semantic search with keyword fallback, all 11 MCP tools, token checks, and the failover demo. Those carry the course grade.

## Open items to settle in phase 00

- The environment is the AWS Academy Cloud Architecting 3 sandbox (ECR and EFS are blocked, so images travel through S3 and the database is snapshotted to S3; see `docs/PLAN.md`, "Sandbox changes"). Phase 00 re-checks that with the laptop credentials and settles whether Session Manager works and whether `LabInstanceProfile` exists.
- Confirm Oct 14 as the demo date.
- Does the instructor expect specific deliverables (Pricing Calculator estimate, written Well-Architected review)? If yes, add them to phase 17.
