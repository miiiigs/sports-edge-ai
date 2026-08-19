# Football Dashboard V1

Last updated: Wednesday, August 19, 2026

## Purpose

This is the first operational football dashboard surface for Sports Edge AI.

It is not a historical report viewer.

It is a local FastAPI application that:

- connects to the current fixture provider
- normalizes live / today / upcoming football fixtures
- builds or loads a current forward football model snapshot
- generates frozen pre-match football predictions server-side
- derives Day 10 fair prices from the frozen model objects
- displays current football matches and predictions in a browser

## Current operational state

Verified locally on Wednesday, August 19, 2026:

- dashboard server boots successfully
- provider state is visible
- fixture sync runs
- current model snapshot builds successfully
- prediction engine is ready
- current odds feed is not connected
- point-in-time xG feed is missing
- real API-Football feed is connected for the configured leagues, but the live/today/upcoming
  in-scope slate returned `0` fixtures at verification time

Current product status flags:

- `DASHBOARD UI READY = YES`
- `CURRENT FIXTURE FEED READY = YES`
- `CURRENT PREDICTION PIPELINE READY = YES`
- `CURRENT MARKET DECISION READY = NO`
- `LIVE_PAPER_PIPELINE_READY = NO`

## Startup

From repo root:

```powershell
$env:PYTHONPATH='code/src'
python -m uvicorn sports_ai.dashboard.app:app --host 127.0.0.1 --port 8008
```

Local URL:

`http://127.0.0.1:8008`

## Provider

Current fixture provider:

- `API-Football`

Required environment variables:

- `SPORTS_AI_API_FOOTBALL_TOKEN`
- `SPORTS_AI_API_FOOTBALL_LEAGUE_MAP`

Expected league-map file at current repo verification time:

- `research/manual_inputs/api_football_league_map.json`

Supported configured divisions in the current dashboard path:

- `E0`
- `E1`
- `E2`
- `E3`
- `SP1`

If credentials are absent, the dashboard does not fabricate fixtures. It surfaces:

- `FIXTURE PROVIDER NOT CONFIGURED`

## Current model snapshot

The dashboard builds a current forward football snapshot from the loaded historical archive by:

1. loading the fitted Day 7 GAP baselines and learning rates
2. replaying completed historical football events chronologically
3. fitting current dashboard-side Day 8 conversion coefficients from completed data
4. fitting current dashboard-side corner distribution selection / dispersion from completed data
5. fitting current dashboard-side Day 9 `rho` from completed data
6. freezing snapshot lineage and `model_as_of`

Current verified snapshot metadata:

- `snapshot_id = current_snapshot_77b65ccf14b446c1f7aa52d3daed76a512b2d1070c05fdf0d84ee7f7cfd6f378`
- `model_as_of = 2026-05-24T23:59:59+00:00`

## Prediction behavior

Predictions are generated server-side and persisted.

Prediction lifecycle statuses:

- `PENDING`
- `RUNNING`
- `READY`
- `BLOCKED`
- `FAILED`
- `STALE`

Pre-match lock policy in Dashboard V1:

- the prediction remains a frozen pre-match object
- live fixtures display the frozen pre-match prediction beside the live score/status
- the dashboard does not recalculate in-play win probabilities from live score

## Pages

- `/` dashboard home
- `/matches`
- `/matches/{fixture_id}`
- `/predictions`
- `/candidates`

## Read APIs

- `/api/fixtures/live`
- `/api/fixtures/today`
- `/api/fixtures/upcoming`
- `/api/fixtures/{fixture_id}`
- `/api/predictions/{fixture_id}`
- `/api/candidates`
- `/api/system/status`

## Persistence

Dashboard V1 stores local runtime state in SQLite:

- normalized fixtures
- persisted prediction versions
- team-resolution traces
- dashboard runtime status

Current path:

- `research/product_outputs/dashboard/football_dashboard.sqlite`

## What is live now

Implemented and real:

- current fixture provider integration
- current fixture normalization
- current forward football snapshot
- server-side frozen pre-match prediction generation
- Day 10 fair-price display
- dashboard UI and read APIs

Not live yet:

- current odds-market ingestion for Day 11 runtime decisions
- point-in-time-valid xG feed for Day 12 runtime safeguards
- live paper output readiness

## Known blockers

- current odds feed is not connected, so current Day 11 decision output remains unavailable
- point-in-time-valid xG is missing, so current Day 12 safeguard output remains unavailable
- supported provider feed was connected at verification time but returned zero in-scope fixtures for
  the configured football competitions

## Demo flow

1. start the dashboard
2. open `/`
3. verify fixture-feed and prediction-engine status badges
4. review `Live Now`, `Today`, `Upcoming`, and `Next Matches`
5. open `/matches`
6. click a fixture when one is available
7. inspect expected shots, SOT, corners, goals, top scorelines, and fair prices
8. confirm manager/safeguard states are shown honestly rather than fabricated
