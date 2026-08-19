# Football Dashboard V1.1

Last updated: Wednesday, August 19, 2026

## Purpose

Dashboard V1.1 keeps the same football-only operational surface as V1, but hardens the current
runtime around real fixture discovery, provider diagnostics, frozen pre-match prediction lock, and
honest degraded-state reporting.

The bounded runtime remains:

`real fixture provider -> normalized fixture -> current forward football snapshot -> frozen pre-match prediction -> Day 10 fair prices -> dashboard/API`

## Current milestone

- `CURRENT PRODUCT MILESTONE = FOOTBALL DASHBOARD V1.1 CURRENT RUNTIME VALIDATION`
- `TENNIS DEVELOPMENT = PAUSED`

## Verified baseline

- full suite before this iteration: `200 passed in 37.43s`
- full suite after this iteration: `202 passed in 29.50s`

## Runtime verification

Verified locally on Wednesday, August 19, 2026:

- startup command:

```powershell
$env:PYTHONPATH='code/src'
python -m uvicorn sports_ai.dashboard.app:app --host 127.0.0.1 --port 8008
```

- local URL: `http://127.0.0.1:8008`
- verified HTTP `200`:
  - `/`
  - `/matches`
  - `/predictions`
  - `/candidates`
  - `/api/system/status`
  - `/api/fixtures/today`
  - `/api/fixtures/upcoming`
  - `/api/fixtures/next`

## Provider truth

Current fixture provider:

- `API-Football`

Configured dashboard competition map:

- `E0 -> 39`
- `E1 -> 40`
- `E2 -> 41`
- `E3 -> 42`
- `SP1 -> 140`

Current-season verification:

- API-Football season identifier for August 2026 is `2026`
- the provider is authenticated
- the current configured plan blocks current-season fixture rows for these competitions

Verified real provider error:

- `Free plans do not have access to this season, try from 2022 to 2024.`

Therefore Dashboard V1.1 now reports the fixture feed as:

- `DEGRADED`

rather than silently treating the current window as a legitimate empty slate.

## Fixture discovery behavior

The dashboard now persists and exposes fixture-sync diagnostics including:

- requested seasons and league ids
- query timestamps
- provider response status
- raw fixture counts for scoped season queries
- normalized fixture counts
- supported / unresolved / blocked counters
- last attempted sync
- last successful sync
- next scheduled refresh
- most recent provider error

Dashboard sections remain:

- `LIVE NOW`
- `TODAY`
- `UPCOMING`
- `NEXT MATCHES`

If the provider is degraded, the UI now shows an honest unavailable state instead of implying that
zero fixtures were definitively discovered.

## Prediction behavior

Predictions remain frozen pre-match objects only.

V1.1 adds or hardens:

- prediction lock metadata:
  - `prediction_locked_at`
  - `prediction_version`
- kickoff lock enforcement:
  - ordinary polling will not create a brand-new prediction after kickoff if no pre-match version
    already existed
- live behavior:
  - fixture state, score, and elapsed minute can update
  - the pre-match model output remains locked

## Refresh behavior

The dashboard now includes:

- `POST /api/fixtures/refresh`
- a simple `Refresh Fixtures` control
- cooldown protection to avoid uncontrolled repeated provider requests

The scheduler remains lightweight and configurable, with live refresh separated from broader future
schedule refresh.

## Read APIs

- `GET /api/fixtures/live`
- `GET /api/fixtures/today`
- `GET /api/fixtures/upcoming`
- `GET /api/fixtures/next`
- `GET /api/fixtures/{fixture_id}`
- `GET /api/predictions`
- `GET /api/predictions/{fixture_id}`
- `GET /api/system/status`
- `POST /api/fixtures/refresh`

## Current limitations

- no current/live odds provider is connected
- no point-in-time-valid xG feed is connected
- `CURRENT MARKET DECISION READY = NO`
- `LIVE_PAPER_PIPELINE_READY = NO`
- because the configured API-Football plan blocks the current season, this run did not validate a
  real current supported fixture flowing all the way to a persisted current prediction

## Practical meaning

Dashboard V1.1 now proves the operational runtime can:

- boot the real football dashboard
- expose honest provider health
- expose fixture-sync diagnostics
- keep frozen pre-match predictions locked correctly
- distinguish provider degradation from a genuinely empty slate

It does not yet prove:

- a real current supported API-Football fixture reaching the full prediction path under the current
  provider plan
- current odds-aware Day 11 decisions
- current xG-aware Day 12 safeguards
