# Railway cost optimization

When the Football Railway service is awake, it runs one low-memory web process and one scheduler process, but only the scheduler owns the full historical model snapshot. Production may use Railway sleeping between scheduled wake-ups so the full runtime does not consume resources continuously.

The web child uses `sports_ai.dashboard.low_memory_app:app`. It serves persisted fixtures, predictions, decisions, paper-trading state, performance outputs and runtime state exactly as before, while reconstructing only a tiny snapshot metadata view from the persisted prediction ledger. The web child does not replay GAP history, refit conversion/corner models, load Route A artifacts for inference, call providers, or generate predictions.

The scheduler child remains `sports_ai.dashboard.scheduler_worker` and is unchanged. It continues to build the full model, run the configured fixture/market cadence, generate predictions, persist decisions, settle paper positions and maintain provider/runtime state.

This optimization is performance-neutral by design: model inputs, parameters, thresholds, provider cadence, xG, Dixon-Coles pricing, context vetoes, portfolio selection and settlement are unchanged. The change removes duplicate in-memory model state only.


## Serverless wake mode

Production can keep Railway sleeping enabled and use `.github/workflows/football-production-wake.yml` to wake the public `/health` endpoint once per hour.

The wake request itself does not perform provider work. Starting the container launches the existing scheduler worker, whose normal startup path runs one full fixture/prediction synchronization and persists the result to `/data`. Railway can then return the service to sleep after the idle window.

Recommended serverless production controls:

- keep Railway `sleepApplication=true`
- keep one Railway replica
- set `SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS=1` only when live provider ingestion is intended
- use `SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS=900`
- use `SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS=3600`
- retain the API-Football operational daily cap, persistent request cache, quota governor, rate limiter and circuit breaker

The longer in-process refresh intervals are deliberate in serverless mode: the hourly cold-start synchronization is the primary production cadence, so a short 5-minute loop would usually spend provider quota and Railway compute on a second refresh shortly before the service goes back to sleep.

If the product later requires continuous live-match tracking, disable Railway sleeping intentionally and revisit the refresh cadence. Do not silently turn the service into an always-on worker.
