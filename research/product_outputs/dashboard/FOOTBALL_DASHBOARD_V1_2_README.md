# Football Dashboard V1.2

Last updated: Wednesday, August 19, 2026

## Purpose

Dashboard V1.2 keeps the accepted frozen football model path intact and refreshes the operational
surface around the requested football-data.org fixture source and a more product-like watchlist UI.

The bounded runtime remains:

`football-data.org fixture provider -> normalized fixture -> current forward football snapshot -> frozen pre-match prediction -> Day 10 fair prices -> dashboard/API`

## Current milestone

- `CURRENT PRODUCT MILESTONE = FOOTBALL DASHBOARD V1.2 FOOTBALL-DATA.ORG WATCHLIST REFRESH`
- `TENNIS DEVELOPMENT = PAUSED`

## Verified baseline

- full suite before this iteration: `202 passed, 1 warning`
- full suite after this iteration: `203 passed, 1 warning`

## What changed

- default dashboard fixture source changed from `API-Football` to `football-data.org`
- added a dedicated `football-data.org` provider adapter around `GET /v4/matches`
- added lightweight provider response caching and richer sync diagnostics
- added a real `/status` / `/system-status` page
- improved the dashboard home, matches, predictions, candidates, and match-detail surfaces into a
  denser watchlist/trading-terminal style layout
- expanded current `next matches` handling so it includes the nearest future fixtures, not only
  next-day rows

## Runtime verification

Verified locally on Wednesday, August 19, 2026:

- dashboard app booted successfully under Uvicorn in-process
- verified HTTP `200`:
  - `/status`
  - `/api/system/status`
- runtime status returned:
  - `fixture_feed.provider = football-data.org`
  - `fixture_feed.state = NOT_CONFIGURED`
  - `prediction_engine.state = READY`

## Provider truth

Current fixture provider:

- `football-data.org`

Required environment variable:

- `SPORTS_AI_FOOTBALL_DATA_TOKEN`

Current verified local runtime truth:

- the dashboard now points at the requested `football-data.org` source
- the local environment used for verification on Wednesday, August 19, 2026 does not currently set
  `SPORTS_AI_FOOTBALL_DATA_TOKEN`
- therefore the dashboard honestly reports:
  - `fixture_feed.state = NOT_CONFIGURED`
- the app does not fabricate live, today, upcoming, or next fixtures when the token is absent

## Product surface

Pages now exposed:

- `/`
- `/matches`
- `/matches/{fixture_id}`
- `/predictions`
- `/candidates`
- `/status`
- `/system-status`

Read APIs:

- `/api/fixtures/live`
- `/api/fixtures/today`
- `/api/fixtures/upcoming`
- `/api/fixtures/next`
- `/api/fixtures/{fixture_id}`
- `/api/predictions`
- `/api/predictions/{fixture_id}`
- `/api/candidates`
- `/api/system/status`
- `POST /api/fixtures/refresh`

## Current limitations

- `SPORTS_AI_FOOTBALL_DATA_TOKEN` is not configured in the verified local environment
- no current/live odds provider is connected
- no point-in-time-valid xG feed is connected
- `CURRENT MARKET DECISION READY = NO`
- `LIVE_PAPER_PIPELINE_READY = NO`

## Practical meaning

Dashboard V1.2 now proves the operational football UI can boot with the requested provider
architecture, show honest status/configuration truth, and present the accepted Sports Edge output in
a stronger watchlist product surface.

It does not yet prove a real current football-data.org fixture flowing into a persisted current
prediction in this local environment, because the required token is not configured.
