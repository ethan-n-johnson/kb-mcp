# Phase 22: Stateless app and scale-out

**Days:** Thu Nov 5 to Fri Nov 6 · **Depends on:** 21 · **Where:** local, then AWS · **PLAN.md sections:** Course alignment (Elasticity, High availability); AWS deployment

## Goal

Any instance can answer any request, so the Auto Scaling group runs 2 to 4 instances across both AZs and scales on CPU. A redeploy no longer takes the service down.

## Read first

- `CLAUDE.md` ("Runtime rules"; several change in this phase), `docs/PROGRESS.md`
- `kb/uploads.py`, `kb/ratelimit.py`, `kb/search/state.py` (`fallback_toggle`), `kb/service.py` (`set_fallback`)
- `infra/app.yaml` (`AppAsg`, `UpdatePolicy`, alarms, `HealthCheckGracePeriod`)

## State that lives in one process today

| State | Today | Phase 22 (PostgreSQL mode) |
| --- | --- | --- |
| Database and vector index | SQLite file and numpy matrix | RDS (phases 20, 21) |
| Upload tickets | `TicketStore` in memory | `upload_tickets` table |
| Fallback toggle | `SearchState.fallback_toggle` | `settings` table, read with a 5 s cache |
| Search rate limit | slowapi memory storage | stays per instance; documented (each instance allows `KB_SEARCH_RATE`) |
| Models | loaded per process | unchanged, one copy per instance |

## Tasks

1. **Upload tickets in the database.** Table `upload_tickets(token_hash PRIMARY KEY, actor, filename, title, tags, expires_at, used_at)`. Store a SHA-256 of the ticket, never the ticket. `take()` is one statement: `UPDATE ... SET used_at = now() WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING ...`, so two instances can never both accept one ticket. Keep the 50-pending cap with a count query. Prune expired rows on issue. SQLite mode keeps the in-memory store.
2. **Fallback toggle in the database.** Table `settings(key PRIMARY KEY, value, updated_at)`. `set_fallback` writes it; `SearchState` reads it at most every 5 seconds. A toggle on one instance reaches every instance within 5 s.
3. **Rate limit.** Leave it per instance and say so in `docs/SECURITY_REVIEW.md` (known limitation: the effective limit is `KB_SEARCH_RATE` times the instance count). Add a `SearchRate` stack parameter (default `30/minute`) passed as `-e KB_SEARCH_RATE=...`, so phase 23 can raise it for the load test.
4. **Auto Scaling group** (in `postgres` mode only, through conditions on `DatabaseMode`):
   - `MinSize 2`, `MaxSize 4`, `DesiredCapacity 2`. Never above 4: the sandbox suspends the account past 9 instances.
   - `UpdatePolicy`: `MinInstancesInService: 1`, `MaxBatchSize: 1`, `WaitOnResourceSignals: true`, so a deploy replaces one instance at a time while the other serves.
   - `CreationPolicy` `ResourceSignal.Count` equals `DesiredCapacity`.
   - Target tracking policy on `ASGAverageCPUUtilization`, target 50, `EstimatedInstanceWarmup: 600` (image load plus model load). EC2 CPU is a basic AWS metric, so it works without any instance role permissions.
   - The `kb-unhealthy-host` and `kb-no-healthy-host` alarms stay. Add `kb-capacity-max` on `GroupInServiceInstances >= 4` (needs `MetricsCollection` on the group) so a scale-out sends an email.
5. **ALB.** Leave round robin, no stickiness. Lower the target group `deregistration_delay` to 30 s so scale-in and deploys drain quickly.
6. **`CLAUDE.md` runtime rules.** Rewrite the "1 worker" and "ASG stays at max 1" rules: still 1 uvicorn worker per instance (memory for the models), the group is 1 in `sqlite` mode and 2 to 4 in `postgres` mode, the numpy index exists only in `sqlite` mode, the journal mode rule applies only to SQLite.
7. **Tests.** Two app instances (two `TestClient`s) on one Postgres: a ticket issued on A is accepted once on B and refused on A; the toggle set on A is seen on B within the cache window (patch the clock); `app.yaml` in `postgres` mode has min 2, max 4, `MinInstancesInService` 1, the scaling policy, and the client security group; in `sqlite` mode it is unchanged from phase 1.
8. Deploy with `KB_DATABASE_MODE=postgres`. Check, and save a screenshot of each for the final report:
   - EC2 console: two `kb-app` instances, one in each AZ (if both land in one AZ, say so).
   - Target group: two healthy targets.
   - `upload_file` from Claude Code works (the link may be issued by one instance and used on the other).
   - Redeploy with a trivial change: searches keep answering during the rolling update (run the four-search loop from phase 17 in a second terminal the whole time and count non-200 answers).
9. Failover drill again: mark one instance unhealthy. Expect no failed searches at all this time, since the other instance keeps serving. Record that next to the phase 12 timing.

## Do not

- Raise `MaxSize` above 4 or start extra instances by hand.
- Store upload tickets in plain text.
- Add sticky sessions to hide shared-state bugs.

## Acceptance

- [ ] Two-instance tests pass on Postgres; `pytest -q` passes.
- [ ] Deployed: 2 healthy targets in 2 AZs; screenshots saved.
- [ ] Rolling redeploy with zero failed searches in the loop (or the count recorded with the cause).
- [ ] Failover drill with the second instance serving; result recorded.
- [ ] `CLAUDE.md` rules updated; phase log updated; commit `phase 22: stateless app, 2 to 4 instances`.
