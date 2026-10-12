# Football Production Operations

Last updated: Monday, October 12, 2026

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
- when `SPORTS_AI_DATA_DIR` is set, the dashboard reports `mode = persistent` and
  `persistent_path_configured = YES`

Current operational filenames:

- `football_dashboard.sqlite`
- `football_paper_trading.sqlite`

The app creates the operational data directory automatically and initializes schemas idempotently at startup.

Repeated restarts and deployments must preserve existing data and must not wipe tables.

Current storage diagnostics now report:

- storage mode
- persistence state
- data directory
- data-directory writability
- dashboard DB path
- paper-trading DB path
- research DB path
- operational DB path list
- last DB initialization timestamp

## Railway Volume Setup

Recommended Railway configuration:

1. Open the deployed service in Railway.
2. Add a persistent Volume to the service.
3. Set the mount path to `/data`.
4. Add the environment variable `SPORTS_AI_DATA_DIR=/data`.

Intended result:

- `/data/football_dashboard.sqlite`
- `/data/football_paper_trading.sqlite`

After adding or changing the Railway variable or volume:

1. redeploy the service
2. open `/status`
3. confirm:
   - `Storage -> Persistence State = PERSISTENT`
   - `Storage -> Data Directory = /data`
   - `Storage -> Persistent Path = YES`
   - `Storage -> Dashboard DB = READY`
   - `Storage -> Paper DB = READY`

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
3. distinguishes resolved identity from missing competition-specific model history
4. checks chronology against the current model snapshot
5. creates a frozen pre-match prediction
6. derives Day 10 fair prices
7. persists the prediction idempotently
8. reuses the same prediction on later refreshes if the version key is unchanged

Current prediction blocker taxonomy now distinguishes:

- `COMPETITION_UNSUPPORTED`
- `HOME_TEAM_IDENTITY_UNRESOLVED`
- `AWAY_TEAM_IDENTITY_UNRESOLVED`
- `HOME_MODEL_HISTORY_MISSING`
- `AWAY_MODEL_HISTORY_MISSING`
- `MODEL_SNAPSHOT_UNAVAILABLE`
- `MODEL_STATE_UNAVAILABLE`
- `PREDICTION_AFTER_KICKOFF_BLOCKED`

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
- `SPORTS_EDGE_CLIENT_API_TOKEN` (optional server-to-server read credential)

Current/future market-provider variables:

- `SPORTS_AI_API_FOOTBALL_TOKEN`
- `SPORTS_AI_API_FOOTBALL_LEAGUE_MAP`
- `SPORTS_AI_THE_ODDS_API_KEY` future only, not currently required

## Unified Client Read-Only Service Token

`SPORTS_EDGE_CLIENT_API_TOKEN` is an optional, deployment-managed secret used
only for server-to-server reads by the Unified Sports Edge client. It is not
required for normal dashboard operation and it is never committed to the repo.

When it is set, a server caller may present it as a Bearer credential:

`Authorization: Bearer <SPORTS_EDGE_CLIENT_API_TOKEN>`

and may then read exactly these paths:

- `GET/HEAD /api/candidates`
- `GET/HEAD /api/performance`
- `GET/HEAD /api/system/status`

No other path is authorized by this token. Do not broaden this allowlist.

Responses are the same JSON payloads the owner session receives. Every other
outcome is `401 {"detail": "Authentication required"}`: missing, empty, wrong
or non-Bearer credentials, any other method (`POST`, `PUT`, `PATCH`, `DELETE`,
`OPTIONS`), trailing-slash variants, and every other `/api/*` path. HTML pages,
`/docs`, `/redoc` and `/openapi.json` redirect to `/login`. The Bearer scheme
name is case-insensitive.

Generate the value with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
A configured value shorter than 32 characters is treated as unset, so the token
path fails closed. Configure the same value on the unified client
(`SPORTS_EDGE_CLIENT_API_TOKEN` in the client service). It is a Football-only
credential and is not interchangeable with Tennis owner authentication.

Rules:

- Not a browser login credential. It never replaces or weakens the owner session
  cookie guard, and it does not authenticate a normal user session.
- Not accepted through query parameters. Only the `Authorization` header is
  considered; `token`, `api_key`, and `access_token` query parameters never
  authenticate an otherwise unauthenticated request.
- Not stored in SQLite. The value is held only in the process environment; it is
  never written to the auth database or the session table.
- Must never be exposed client-side. Use it only on the server-to-server
  boundary; do not embed it in browser JavaScript, HTML, or any public client
  code.
- `/health` remains public and does not require this token.

Rotation or removal takes effect on the next request; no database migration is
involved.

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

Stored-current-slate replay verification on Thursday, August 20, 2026 from the local
Wednesday, August 19, 2026 football-data.org snapshot:

- `supported fixtures = 29`
- `team resolution = 49 resolved / 0 unresolved / 7 model-history-missing`
- `predictions ready = 21`
- `predictions blocked = 25`

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
