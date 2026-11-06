# Fix plan: instances killed by the ALB health check under load

Branch `fix-health-under-load` off `phase-23-load-test` (commit 0bed011, not yet merged to master). Rules from CLAUDE.md
apply (no em dashes, no secrets, one commit, record deviations and the fix in docs/PROGRESS.md).

## What went wrong (phase 23 run1, 2026-11-06)

30 Locust users through CloudFront. 502s began about 20 s after full load. Both original instances
were terminated for "ELB system health check failure" (19:13:25 and 19:17:24 UTC), the replacements
needed about 10 min to warm, and 76% of requests failed with 502.

## Root causes (from the code)

1. **No admission control on semantic search.** `kb/search/pipeline.py` `search()` wraps `_semantic` in
   `asyncio.wait_for(..., search_timeout)`. On timeout the coroutine is cancelled, but the embed and
   rerank work already handed to `run_in_threadpool` keeps running in its thread. Offered load
   (about 7 searches/s) far exceeds capacity (about 1 search/s per 2 vCPU t3.medium at about 2.3 s CPU
   each), so abandoned work piles up in the anyio thread pool (40 threads) and every new request,
   including the keyword fallback, queues behind it.
2. **`/healthz` shares the congested resources.** In PostgreSQL mode `kb/main.py` `healthz` runs
   `_count_chunks` through `run_in_threadpool` (same 40-thread limiter as the searches) and opens a
   store connection (`open_store`, the shared pool of max 5 that the searches hold) to run
   `SELECT count(*), count(embedding) FROM chunks` on every check. Under load it misses the ALB's 5 s
   timeout three times in a row (45 s) and the ASG (`HealthCheckType: ELB`) terminates the instance.
   This breaks the rule that load or model trouble must never make the ALB kill an instance.
3. **Keep-alive race (likely contributor to the 502s that appeared before any termination).** uvicorn
   runs with its default `--timeout-keep-alive 5`, below the ALB's default 60 s idle timeout. The ALB can
   reuse a connection the instant uvicorn closes it, which the ALB reports as 502. AWS recommends the
   target keep-alive be longer than the ALB idle timeout.

## Changes

### 1. `/healthz` that cannot be starved (kb/main.py, small helper module if needed)
- A dedicated daemon thread (plain `threading.Thread`, not the anyio pool) started in the lifespan in
  PostgreSQL mode. Every 10 s it opens its **own** connection (not from the shared pool; reuse it while
  it works, reconnect on error, `connect_timeout` short) and runs the existing count query, storing
  `(ok, chunks, indexed, checked_at)` in a small lock-protected object on `app.state`.
- `healthz` reads that object only, never awaits a thread or the pool: 200 with the cached counts if the
  last success is younger than 60 s, else 503 `{"status":"error"}`. Before the first check completes
  (startup), run one check synchronously in the lifespan so the first `/healthz` already has data.
- SQLite mode unchanged (shared connection on the event loop, already fast).
- Keep the JSON shape exactly as it is (tests and the UI depend on it). Add `"checked_at"` age only if
  harmless; otherwise leave it out.
- Docstring update: "200 whenever the database was readable within the last 60 s".

### 2. Admission control for semantic search (kb/search/pipeline.py, kb/config.py)
- New setting `KB_SEMANTIC_CONCURRENCY` (default `2`, must be at least 1; t3.medium has 2 vCPU).
  One `asyncio.Semaphore` per `SearchState`.
- In `search()`: try to acquire the semaphore within the search timeout budget. If it cannot be
  acquired in time, skip semantic and use keyword search immediately (log `"semantic search busy;
  using keyword search"`, same metrics line with `mode=keyword`). Release it in `finally` only after
  the **thread** work has actually finished: do not use `wait_for` to cancel a call that holds the
  semaphore while its thread is still running. Implementation: run `_semantic` as a task, wait with
  `asyncio.wait({task}, timeout=...)`; on timeout return the keyword result to the caller, and
  attach a done-callback to the task that releases the semaphore when it finishes. Net effect: at most
  N semantic computations ever run per instance, so CPU stays high (scaling still triggers) but the
  backlog is bounded.
- The keyword fallback still runs via the thread pool; it is cheap and now does not queue behind
  dozens of abandoned embeds.
- Add the variable to the CLAUDE.md config table and `docs/PLAN.md` if PLAN has a config table;
  record as a deviation in PROGRESS.md.

### 3. Keep-alive longer than the ALB idle timeout (Dockerfile, infra/app.yaml)
- Dockerfile CMD: add `"--timeout-keep-alive", "75"`.
- Set the ALB attribute `idle_timeout.timeout_seconds` explicitly to `60` in `infra/app.yaml`
  (`LoadBalancerAttributes`) so the relationship is visible and testable.

### 4. Not changing (record the reasoning in PROGRESS.md)
- ALB health check thresholds stay (interval 15, timeout 5, unhealthy 3): with a cheap `/healthz`
  they are reasonable, and loosening them would only hide a hung instance longer.
- Pool size stays 1 to 5.

## Tests

### A. Fast tests (pytest, add to existing files)
1. `tests/test_health.py`, mark `dual` or `pg` as fits:
   - `/healthz` returns 200 in under 200 ms while the anyio thread limiter is fully occupied
     (fill `anyio.to_thread.current_default_thread_limiter()` tokens with blocking tasks, or set
     `total_tokens=1` and hold it).
   - PostgreSQL leg: `/healthz` returns 200 while every pool connection is checked out (hold all 5).
   - Returns 503 when the cached check is older than 60 s (inject the clock or the cache object);
     returns 200 again after a successful check.
   - Response JSON keys unchanged.
2. `tests/test_search.py` (with `KB_FAKE_MODELS=1` doubles, add a slow fake or a sleep hook in
   `tests/fakes.py` if none exists):
   - With concurrency 1 and a fake embedder that blocks on an event, a second concurrent search
     returns `mode="keyword"` without waiting for the first.
   - A timed-out semantic search does not release the slot until its thread work finishes (count
     concurrent embed calls in the fake; never above the limit across 20 concurrent searches).
   - Setting `KB_SEMANTIC_CONCURRENCY=0` or garbage fails config validation (`tests/test_config.py`).
3. `tests/test_infra.py`: Dockerfile CMD contains `--timeout-keep-alive` with a value greater than
   the ALB `idle_timeout.timeout_seconds` in `infra/app.yaml`.
4. Full suites: `pytest -q`, and with Docker PostgreSQL
   `KB_TEST_DATABASE_URL=... pytest -q -m "not models"`; `ruff check`; `cfn-lint infra/*.yaml`.

### B. Local load test (laptop, real models, PostgreSQL in Docker), before touching AWS
1. Start `pgvector/pgvector:pg16`, `import-sqlite` the saved snapshot into a `kb` database, run
   `KB_DATABASE_URL=... KB_WRITE_TOKENS=dev:devtoken KB_SEARCH_RATE=6000/minute uvicorn kb.main:app
   --port 8000 --workers 1 --timeout-keep-alive 75`. Limit the container or process to 2 CPUs
   (`taskset -c 0,1`) to resemble a t3.medium.
2. In parallel, a probe loop: `/healthz` once per second with `curl -m 5 -w '%{time_total}'`,
   logging status and time.
3. `locust ... --host http://localhost:8000 --headless -u 30 -r 2 -t 5m --csv <scratch>/local`.
4. Pass: every probe 200 in under 1 s; Locust failure rate 0%; search p95 under 10 s (searches may
   be `keyword` mode under overload, which is fine); app log shows "busy; using keyword search"
   lines, proving admission control engaged.
5. Run the same before the fix (on master) for a baseline, to confirm this test reproduces the
   problem (expect slow or failing probes). If the baseline does not reproduce it, say so; the AWS
   rerun is then the real check.

### B2. Quick reproduction check (cheap, use before and after the fix)
After the phase 23 revert, 4 parallel curl loops of searches through CloudFront gave 43 of 100
requests as 502 and pushed one target to `unhealthy` (docs/PROGRESS.md, Open issues). Script it in
`loadtest/burst.sh` (4 parallel loops, 25 searches each, print status counts and max time). Locally
(against port 8000) and on AWS after the deploy it must give 0 x 502 and no unhealthy target.
Note the deployed rate limit (30/minute per instance) will answer some 429s on AWS; those are
expected and are not failures for this check.

### C. AWS rerun (phase 23 run2; HUMAN runs Locust)
1. Deploy the fixed image: `infra/up.sh` (clean tree, so it builds once) with
   `KB_SEARCH_RATE=6000/minute` in `up.env`. Confirm 2 healthy targets.
2. User runs the same command with `--csv loadtest/results/run2`.
3. Pass criteria, checked by the agent afterwards:
   - `describe-scaling-activities`: **no** "ELB system health check failure" terminations during the
     run.
   - ALB CloudWatch for the run window: `UnHealthyHostCount` max 0, `HTTPCode_ELB_502_Count` sum 0
     (allow a handful only if they line up with a scale-out instance registering, and explain).
   - Locust: failure rate under 1%, `/healthz` p99 under 1 s.
   - At least one scale-out (CPU target 50) and, after the test, one scale-in, with timings.
   - Search responses report a mix of `semantic` and `keyword` modes under load (sample a few with
     curl during the run, or count from the app logs if reachable).
4. Revert `SearchRate` to `30/minute`, update PROGRESS.md (phase 23 run2 numbers next to run1, the
   fix, deviations), commit `fix: health check and admission control under load`.

## Out of scope
- Faster warmup (baking models into the AMI, smaller image): note it as a follow-up if run2 shows
  scale-out arriving too late to help.
- Raising the instance limit above 4 (forbidden).
