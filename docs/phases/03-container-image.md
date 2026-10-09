# Phase 03: Container image and S3 delivery

**Day:** 1 (Wed Oct 7) · **Depends on:** 01, 02 · **Where:** local, AWS (S3 upload) · **PLAN.md sections:** Summary (embeddings, reranker rows); AWS deployment (deploy step 3); Risks (fastembed cross-encoder row)

## Goal

A `linux/amd64` image with both ONNX models baked in, proven to load offline, delivered as a gzipped `docker save` tarball in the S3 data bucket (the sandbox has no ECR; see `docs/PLAN.md`, "Sandbox changes"). This also retires the risk that fastembed cannot load the cross-encoder.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (bucket name)
- `docs/PLAN.md`: "Summary" table, deploy step 3, the fastembed row in the risk table

## Tasks

1. Model check command in `kb/cli.py`: `python -m kb.cli check-models`. It must:
   - load `TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=KB_MODEL_DIR)` and `TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2", cache_dir=KB_MODEL_DIR)` (import from `fastembed.rerank.cross_encoder`);
   - print the embedding dimension (expect 384) and the vector norm;
   - rerank one query against a relevant and an irrelevant passage and print both raw scores and their sigmoid (relevant must be higher);
   - exit non-zero on any failure.
   Use `argparse` subcommands so later phases can add `seed`, `backup`, `restore`, `mcp-stdio`, `eval`.
2. Run `check-models` locally first with `KB_MODEL_DIR=./models`. **If `TextCrossEncoder` is missing or the model is unsupported, stop.** Record it, describe the PLAN fallback (sentence-transformers `CrossEncoder` with CPU-only torch, larger image), and ask the user before switching.
3. `Dockerfile`:
   - `FROM python:3.12-slim`, build with `--platform linux/amd64`.
   - Install the package (copy `pyproject.toml` first for layer caching, then `kb/`).
   - `ENV KB_MODEL_DIR=/models FASTEMBED_CACHE_PATH=/models HF_HUB_OFFLINE=1` (set `HF_HUB_OFFLINE` only after the download step).
   - A `RUN python -c ...` step that downloads both models into `/models`.
   - Copy `seeds/` if it exists (phase 11 adds it; use a `.dockerignore`-safe pattern so the build does not fail without it, for example create an empty `seeds/.keep` now).
   - Non-root user (uid 10001) that owns `/models`; `/data` is a mount point.
   - `EXPOSE 8000`; `CMD ["uvicorn", "kb.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]`.
4. `.dockerignore`: `.venv`, `.git`, `.localdata`, `models`, `tests`, `__pycache__`, `*.db`, `extension`, `docs`, `infra`.
5. Build and test locally:
   - `docker build --platform linux/amd64 -t kb-mcp:dev .`
   - `docker run --rm --network none kb-mcp:dev python -m kb.cli check-models` (proves the models are baked in, no network).
   - `docker run --rm -p 8000:8000 -e KB_DB=/data/kb.db -e KB_WRITE_TOKENS=dev:devtoken -v $PWD/.localdata:/data kb-mcp:dev` and curl `/healthz`. If the non-root user cannot write `.localdata`, fix ownership on the host dir (`chown 10001`) rather than running as root.
6. Upload to S3: tag the image `kb-mcp:<tag>` with `git rev-parse --short HEAD` (plus `-dirty` for an uncommitted tree), then `docker save kb-mcp:<tag> | gzip | aws s3 cp - s3://<bucket>/images/kb-mcp-<tag>.tar.gz`, with the bucket from the `kb-BucketName` export. Check the object with `aws s3api head-object`. Record the tag, the image size and the compressed object size in `docs/PROGRESS.md`. The instance later runs `aws s3 cp ... - | gunzip -c | docker load` (phase 06).
7. Add `infra/push-image.sh` that does step 6 for the current commit (`--build` builds first), removes the object if the upload fails halfway, and prints only the tag on stdout.

## Do not

- Install torch or sentence-transformers unless the user approved the fallback in task 2.
- Bake any token or AWS credential into the image.

## Acceptance

- [ ] `check-models` passes locally and inside the container with `--network none`.
- [ ] The container serves `/healthz` with 200.
- [ ] `docker image inspect kb-mcp:dev --format '{{.Size}}'` recorded; target under about 1.5 GB.
- [ ] `aws s3api head-object --bucket <bucket> --key images/kb-mcp-<tag>.tar.gz` succeeds with a non-zero `ContentLength`, and `aws s3 cp s3://<bucket>/images/kb-mcp-<tag>.tar.gz - | gunzip -c | docker load` prints `Loaded image: kb-mcp:<tag>`.
- [ ] `docker history kb-mcp:dev` shows no secrets.
- [ ] Phase log updated; commit `phase 03: container image and S3 delivery`.
