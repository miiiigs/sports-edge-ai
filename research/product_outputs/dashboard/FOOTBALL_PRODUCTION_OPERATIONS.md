# Football Production Operations

Last updated: Thursday, August 20, 2026

## Scope

This document covers the current football production-hardening state of the operational dashboard.

It does not authorize:

- model retuning
- tennis work
- fake bookmaker integration
- live-betting claims

## Architecture

Current production flow:

`football-data.org fixtures -> normalized fixture rows -> supported-competition check -> team resolution -> current model snapshot -> frozen pre-match prediction -> Day 10 fair prices -> persisted prediction -> kickoff lock -> dashboard display`

Current market odds remain optional.

If no approved bookmaker provider is configured:

- fixtures still sync
- predictions still generate automatically
- fair prices still exist
- paper ledger remains ready
- live paper tracking remains waiting for odds provider

## Storage Model

Immutable reference data:

- `research/football_research/store/football_research.sqlite`
- frozen football research artifacts under `research/football_research/`
- accepted product outputs under `research/product_outputs/football/`

Mutable operational data:

- `football_dashboard.sqlite`
- `football_paper_trading.sqlite`

Storage resolution:

- if `SPORTS_AI_DATA_DIR` is set, mutable operational databases are created there
- otherwise local development falls back to `research/product_outputs/dashboard/`

Current operational filenames:

- `football_dashboard.sqlite`
- `football_paper_trading.sqlite`

The app creates the operational data directory automatically and initializes schemas idempotently at startup.

Repeated restarts and deployments must preserve existing data and must not wipe tables.

## Railway Volume Setup

Recommended Railway configuration:

1. Open the deployed service in Railway.
2. Add a persistent Volume to the service.
3. Set the mount path to `/data`.
4. Add the environment variable `SPORTS_AI_DATA_DIR=/data`.

Intended result:

- `/data/football_dashboard.sqlite`
- `/data/football_paper_trading.sqlite`

The immutable research store should remain packaged in the repo image unless the model-loading architecture is intentionally redesigned later.

## Automatic Sync Behavior

The dashboard service runs an in-process background sync loop when `SPORTS_AI_ENABLE_AUTOMATED_SYNC` is enabled.

Behavior:

- one scheduler task per app process
- serialized syncs via an async lock
- startup sync runs automatically
- live window refresh uses a shorter cadence
- future schedule refresh uses a slower cadence
- manual refresh remains cooldown-protected
- exceptions are captured and logged without crashing the app

Current environment variables:

- `SPORTS_AI_ENABLE_AUTOMATED_SYNC`
- `SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS`
- `SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS`
- `SPORTS_AI_MANUAL_REFRESH_COOLDOWN_SECONDS`
- `SPORTS_AI_UPCOMING_HORIZON_DAYS`

Important deployment note:

- the current scheduler design assumes a single app replica for clean ownership
- if Railway later runs multiple replicas, scheduler ownership must be revisited or explicitly externalized

## Prediction Automation

For each eligible future fixture the service now:

1. validates supported competition scope
2. resolves team identity deterministically
3. checks chronology against the current model snapshot
4. creates a frozen pre-match prediction
5. derives Day 10 fair prices
6. persists the prediction idempotently
7. reuses the same prediction on later refreshes if the version key is unchanged

Prediction identity is based on:

- fixture id
- model snapshot id
- kickoff

Kickoff rule:

- no first-ever pre-match prediction may be created after kickoff
- if a prediction already exists, live score/status updates must not mutate the frozen prediction payload

## Healthcheck

Production health endpoint:

- `GET /health`

It is designed to return quickly and does not require a live third-party API response at request time.

It reports:

- process readiness
- local database reachability
- snapshot readiness
- whether persistent storage has been configured

## Required Variables

Current required variable for real football fixture sync:

- `SPORTS_AI_FOOTBALL_DATA_TOKEN`

Recommended operational variable:

- `SPORTS_AI_DATA_DIR`

Useful optional runtime controls:

- `SPORTS_AI_ENABLE_AUTOMATED_SYNC`
- `SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS`
- `SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS`
- `SPORTS_AI_MANUAL_REFRESH_COOLDOWN_SECONDS`
- `SPORTS_AI_UPCOMING_HORIZON_DAYS`
- `SPORTS_AI_CURRENT_XG_CONNECTED`
- `SPORTS_AI_CURRENT_ODDS_CONNECTED`

Current/future market-provider variables:

- `SPORTS_AI_API_FOOTBALL_TOKEN`
- `SPORTS_AI_API_FOOTBALL_LEAGUE_MAP`
- `SPORTS_AI_THE_ODDS_API_KEY` future only, not currently required

## Known External Dependencies

Current fixture source:

- `football-data.org`

Current odds state:

- `CURRENT_ODDS_PROVIDER = NOT_CONFIGURED`

This is not treated as a critical dashboard failure.

Current expected honest UI state:

- `MARKET ODDS = NOT CONNECTED`
- `PAPER MARKET TRACKING = WAITING FOR ODDS PROVIDER`

## Real Current Prediction Verification

As of Thursday, August 20, 2026:

- `SPORTS_AI_FOOTBALL_DATA_TOKEN` is not present in the verified local environment
- therefore a real football-data.org fixture sync and first genuine current fixture-to-prediction validation cannot be completed from this environment

This means:

- the automation path is implemented
- mock-backed and local runtime verification is complete
- real current football-data.org verification remains blocked by missing credentials, not by code design

Do not fabricate `FIRST_CURRENT_PREDICTION_VALIDATION.md` without a genuine eligible live fixture run.

## Railway Sleep / Serverless Note

Continuous automated fixture and prediction processing requires the service process to stay awake.

If Railway serverless sleep or similar idle suspension is enabled:

- background sync cadence will not run continuously while the app is asleep

Operational recommendation:

- keep the football dashboard on a continuously running service mode if continuous sync is required

## Verification Checklist

For a production verification run:

1. confirm `SPORTS_AI_DATA_DIR` points to persistent storage
2. confirm `SPORTS_AI_FOOTBALL_DATA_TOKEN` is present
3. boot the app
4. hit `/health`
5. hit `/api/system/status`
6. confirm fixture sync timestamps advance
7. confirm upcoming supported fixtures appear
8. confirm a frozen prediction row persists before kickoff
9. confirm later refreshes do not duplicate the prediction
10. confirm live status updates do not mutate the frozen prediction payload
