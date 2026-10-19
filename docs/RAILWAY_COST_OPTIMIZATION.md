# Railway cost optimization

When the Football Railway service is awake, it runs one low-memory web process and one scheduler process, but only the scheduler owns the full historical model snapshot. Production may use Railway sleeping between scheduled wake-ups so the full runtime does not consume resources continuously.

The web child uses `sports_ai.dashboard.low_memory_app:app`. It serves persisted fixtures, predictions, decisions, paper-trading state, performance outputs and runtime state exactly as before, while reconstructing only a tiny snapshot metadata view from the persisted prediction ledger. The web child does not replay GAP history, refit conversion/corner models, load Route A artifacts for inference, call providers, or generate predictions.

The scheduler child remains `sports_ai.dashboard.scheduler_worker` and is unchanged. It continues to build the full model, run the configured fixture/market cadence, generate predictions, persist decisions, settle paper positions and maintain provider/runtime state.

This optimization is performance-neutral by design: model inputs, parameters, thresholds, provider cadence, xG, Dixon-Coles pricing, context vetoes, portfolio selection and settlement are unchanged. The change removes duplicate in-memory model state only.


## Serverless wake mode

Production can keep Railway sleeping enabled and use `.github/workflows/football-production-wake.yml` to wake the public `/health` endpoint once per hour.

The wake request itself does not perform provider work. Starting the container launches the existing scheduler worker, whose normal startup path runs one full fixture/prediction synchronization and persists the result to `/data`. Railway can then return the service to sleep after the idle window.

`GET /health` is a liveness response from the read-only web child. It can return HTTP 200 before the scheduler process starts, while the startup sync is still running, or after the startup sync failed. Do not treat HTTP 200 alone as evidence of a useful wake.

Use `GET /health/wake?since=<ISO-8601>` to verify useful processing. The scheduler persists a durable `wake_evidence_state` record after every cycle with:

- `startup_sync_state` and completion/error timestamps;
- `last_cycle_state` (`RUNNING`, `COMPLETED`, `DEGRADED`, `FAILED`, `SKIPPED`);
- fixture and prediction counts;
- `forward_predictions_new_last_cycle` and `forward_predictions_new_ready_last_cycle`;
- an explicit `forward_predictions_noop_reason` when no new forward prediction was needed;
- a compact provider-budget snapshot.

`/health/wake` returns HTTP 200 only when a useful cycle completed at or after `since`; otherwise it returns HTTP 503 with `status=pending` or `status=failed`. It is read-only, makes zero provider requests, and does not trigger a sync.

### Verified Railway cron command

The legacy `football-hourly-wake-v2` service uses `alpine:3.20`, whose default command is `/bin/sh`. The observed hourly `Starting Container`-only logs are consistent with Railway passing the configured start command as arguments to that image command: a start command beginning with `sh` becomes `/bin/sh sh -ec '...'`, which exits with `can't open 'sh'` before `wake-start` is printed and before any HTTP request is made. That argument form was reproduced locally with the same Alpine image and produces exactly the missing-log/no-request behaviour.

For the existing Alpine image, configure the start command without the leading `sh` and verify the run logs show `wake-start` and `wake-ok`:

```text
-ec 'echo wake-start; wget -S -T 90 -O /dev/null https://sports-edge-ai-production.up.railway.app/health; echo wake-ok'
```

The reliable option is the repo-contained runner, which also waits for `/health/wake` instead of stopping at HTTP 200:

- Dockerfile: `ops/football_wake/Dockerfile`
- script: `ops/football_wake/wake_and_verify.sh`

Point the cron service at this repository, set the Dockerfile path to `ops/football_wake/Dockerfile`, keep `20 * * * *`, and clear the custom start command. Each run then logs `wake-start`, `wake-http-ok`, polling results and `wake-ok`/`wake-failed`, and exits non-zero when a useful cycle was not verified. This is a production Railway configuration change and requires owner approval.

Recommended serverless production controls:

- keep Railway `sleepApplication=true`
- keep one Railway replica
- set `SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS=1` only when live provider ingestion is intended
- use `SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS=900`
- use `SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS=3600`
- retain the API-Football operational daily cap, persistent request cache, quota governor, rate limiter and circuit breaker
- persist the six-hour current-season research refresh cadence across serverless cold starts, so hourly wakes do not redownload the same five league feeds

The longer in-process refresh intervals are deliberate in serverless mode: the hourly cold-start synchronization is the primary production cadence, so a short 5-minute loop would usually spend provider quota and Railway compute on a second refresh shortly before the service goes back to sleep.

If the product later requires continuous live-match tracking, disable Railway sleeping intentionally and revisit the refresh cadence. Do not silently turn the service into an always-on worker.
