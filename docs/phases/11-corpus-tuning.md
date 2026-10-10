# Phase 11: Seed corpus and tuning

**Day:** 5 (Sun Oct 11) · **Depends on:** 09 (10 optional) · **Where:** local, then on the instance · **PLAN.md sections:** Demo corpus and demo script; Search (confidence table); Ingestion (duplicate rules)

## Goal

About 60 real documents across all input formats, the planted demo cases, an idempotent `seed` command, and confidence and duplicate thresholds tuned against about 20 test questions.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md`
- `docs/PLAN.md`: "Demo corpus and demo script" (both tables and the 10-step script), the confidence table in "Search"

## Tasks

1. `seeds/seeds.yaml` manifest. Entry fields: `url` or `path`, `format` (optional), `title` (optional), `tags`. Sections:
   - About 30 AWS user guide pages for VPC, EC2, Auto Scaling, ELB, EFS, S3, CloudFront, CloudFormation. Must include pages that answer the demo questions: how an Auto Scaling group replaces an unhealthy instance, and how to mount EFS on Amazon Linux.
   - About 20 FastAPI docs Markdown files from `https://raw.githubusercontent.com/fastapi/fastapi/master/docs/en/docs/` (tutorial pages).
   - About 5 MCP specification pages from `modelcontextprotocol.io` (use the latest spec version; check the site for it).
   - 1 or 2 AWS whitepaper PDFs. Check page count (at most 300) and conversion quality with `python -m kb.cli convert`; pick a Well-Architected pillar whitepaper if the full framework PDF is too long.
   **Verify every URL** returns 200 and converts to sensible chunks before committing the manifest. Replace dead links; never guess.
2. Extend `clean_markdown` for what the real corpus shows. FastAPI docs contain mkdocs syntax such as `{* ../../docs_src/... *}` include lines and `/// tip` admonition fences; drop the include lines and turn admonitions into plain paragraphs. Add a test per new rule.
3. Planted cases:
   - `seeds/planted/fastapi-near-duplicate.md`: a lightly edited copy (reworded intro, one paragraph removed) of the seeded FastAPI "Handling Errors" page (was an EC2 page; see Deviations in PROGRESS.md). **Not ingested by `seed`**; it is uploaded live in demo step 4.
   - `planted.stale`: 3 seeded URLs whose `verified_at` and `updated_at` the seeder backdates by 200 days.
4. `python -m kb.cli seed seeds/seeds.yaml`: ingests each entry through `service.upload` / `service.ingest_url` with actor `cli`; skips entries whose `source_url` already exists or whose content hash matches (idempotent); applies the stale backdating; prints a summary table (added, skipped, failed with reason). One failing URL must not stop the run.
5. Make sure `seeds/` is copied into the image (phase 03 left a placeholder).
6. Run the seed locally against `.localdata/kb.db` first; fix converter problems found there.
7. Evaluation set `tests/eval/questions.yaml`, about 20 questions: 14 answerable (including the demo questions, expected `source_url` for each), 3 paraphrased (no shared keywords with the answer), 3 outside the corpus (expected `low`).
   **HUMAN:** show the user the question list and let them edit it before tuning.
8. `python -m kb.cli eval tests/eval/questions.yaml`: for each question print rank of the expected doc, top score, confidence and mode; then hit@1, hit@5, and the top-score ranges for answerable vs out-of-corpus questions. Also run the near-duplicate check (dry run, no write) for the planted file and print its similarity to the original and the highest similarity between any two distinct seeded docs.
9. Tune:
   - `KB_CONF_HIGH` / `KB_CONF_LOW` so out-of-corpus questions are `low` and most answerable ones are `high`.
   - `KB_DUP_THRESHOLD` between the highest distinct-doc similarity and the planted duplicate's similarity.
   Update the defaults in `kb/config.py`, record the values and the eval summary in `docs/PROGRESS.md`.
10. Seed on AWS. The sandbox instances have no SSM (deviation 8), so seed on the laptop into `.localdata/seed/kb.db` (run it twice to prove idempotency), upload it with `python -m kb.cli snapshot-push`, then deploy so the replacement instance restores it at boot. Steps in `docs/SETUP.md`, "Seed the demo corpus".

## Do not

- Ingest the planted near-duplicate during seeding.
- Commit downloaded corpus content; the manifest holds URLs only.

## Acceptance

- [ ] Seed run adds about 60 docs; the second run adds 0; the deployed instance serves them after restoring the snapshot.
- [ ] Eval: hit@5 at least 90% on answerable questions; all 3 out-of-corpus questions are `low`; both demo questions are `high` with the right doc at rank 1.
- [ ] `GET /api/stale` returns the 3 planted docs.
- [ ] Uploading `seeds/planted/fastapi-near-duplicate.md` (dry run or against a local copy) returns `possible_duplicate` with the original as top candidate.
- [ ] Tuned values recorded in PROGRESS.md; phase log updated; commit `phase 11: seed corpus and tuning`.
