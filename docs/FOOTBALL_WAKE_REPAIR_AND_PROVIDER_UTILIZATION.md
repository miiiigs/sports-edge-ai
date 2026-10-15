# Football: wake-service repair and provider utilization (2026-10-15)

Read-only diagnosis. No Railway configuration was changed, no service was redeployed or woken, and no paid
API calls were made.

**Evidence:**
- `railway status --json` and `railway logs --deployment` for both wake services (2026-10-15).
- `.github/workflows/football-production-wake.yml` and its `gh run list` history.
- The frozen production read of 2026-10-13 03:34Z (`provider_request_cache`, `dashboard_fixtures`,
  `dashboard_runtime_state`, `football_model_pick_entries`).
- Code on `integration/football-m1-rc-20261013` (#122, `96d6636`) and on `main` (`6c67200`).

**CONFIRMED** means verified directly from the evidence above. **SUSPECTED** means inferred and not yet
verified.

## 1. Wake services

### 1.1 Current configuration (CONFIRMED, `railway status --json`)

| Service | Kind | Image | Service-level start command | Start command in the *active deployment* manifest | Cron | Restart | Deployment created |
|---|---|---|---|---|---|---|---|
| `football-hourly-wake` | service (shows "Online") | `curlimages/curl:8.16.0` | `--fail --silent --show-error --location --retry 2 --retry-delay 5 --max-time 90 https://sports-edge-ai-production.up.railway.app/health` | **None** | none | NEVER | 2026-09-26 00:08Z |
| `football-hourly-wake-v2` | cron job | `alpine:3.20` | `sh -ec 'echo wake-start; wget -S -T 90 -O /dev/null https://sports-edge-ai-production.up.railway.app/health; echo wake-ok'` | **None** | `20 * * * *` | NEVER | 2026-09-26 08:40Z |
| `sports-edge-ai` | web + scheduler | Dockerfile | n/a | n/a | n/a | ON_FAILURE | 2026-10-14 08:39Z, `sleepApplication=True` |

### 1.2 Root cause (CONFIRMED)

Both wakers fail for the same reason. The start command was edited on the service *after* the only
deployment was built, and neither service was redeployed afterwards. A Railway deployment keeps the
manifest it was built with, so the active deployments still have `startCommand = None` and run the image
default:

- **`football-hourly-wake`** runs the curl image default, which is bare `curl` with no arguments. Its log
  contains exactly one line: `curl: try 'curl --help' or 'curl --manual' for more information`.
  - There is no cron schedule, so it never runs again.
  - The bare-argument command would itself only work if Railway appended it to the image entrypoint.
    Whether it does is SUSPECTED, not verified.
- **`football-hourly-wake-v2`** runs alpine's default `CMD ["/bin/sh"]`. With no stdin or TTY, the shell
  exits 0 immediately.
  - The logs therefore show only `Starting Container` on every hourly run (449 so far), and never
    `wake-start`.
  - The cron schedule itself works.

### 1.3 GitHub schedule (CONFIRMED)

`football-production-wake.yml` (cron `17 * * * *`) succeeds on every run. GitHub throttles scheduled
workflows, though: between 10-05 and 10-15 it fired only 4–6 times a day, with gaps of 3.5–7.6 h.

This is the only working waker today. It explains the roughly 5 production wakes a day.

### 1.4 Exact repair plan (owner action in the Railway console; needs authorization)

1. **`football-hourly-wake-v2`, the only waker to keep:**
   - Keep image `alpine:3.20`, cron `20 * * * *` and restart policy `NEVER`.
   - Set the start command (busybox wget, which needs no extra package):
     ```
     sh -ec 'echo wake-start; wget -S -T 90 -O /dev/null https://sports-edge-ai-production.up.railway.app/health; echo wake-ok'
     ```
   - **Redeploy the service.** This is the step that was missing.
2. **Verify on the next :20.** `railway logs --service football-hourly-wake-v2` must show `wake-start`, an
   `HTTP/1.1 200` header and `wake-ok`. `railway logs --service sports-edge-ai` must show a startup or
   health request at about :20. Confirm this over 3 consecutive hours.
3. **Delete `football-hourly-wake`** only after step 2 passes. It has no schedule, and its single run failed.
4. **Keep the GitHub workflow** as an independent backup waker. It is free, and the two schedules overlap
   harmlessly.
5. **Rollback:** clear the v2 cron schedule, or delete the service. Production then falls back to the
   GitHub waker, which is the current behaviour.

**Why `:20` (CONFIRMED arithmetic):** an hourly wake at `:20` falls 10–40 minutes before every `:00`, `:30`
and `:45` kickoff (12:30→12:20, 15:00→14:20, 19:45→19:20, 20:00→19:20). That meets #113's 60-minute CLV
closing rule. Closing prices are only collected, though, when the fixture is selected for market
collection (see §2.3).

### 1.5 Alternative: turn off sleeping (`sleepApplication=False`)

- **Memory:** the scheduler's high-water mark is 1,285 MiB and the web process about 87 MiB, so roughly
  1.4 GB would be held continuously instead of for about 1–2 h a day. **SUSPECTED cost:** about 1 GB-month
  of memory plus idle CPU, on the order of US$10–20 a month at Railway usage pricing. Check the current
  invoice first.
- **API budget, SUSPECTED estimate:**
  - With every loop at its 600 s default: market collection about 144 cycles × up to 5 requests, result
    sync about 144 × 2, plus fixture sync.
  - The total is still under the 4,500 operational cap, but well above today's 228 a day.
- **Risk:** the unbounded scheduler memory growth (the separate memory audit item) would then accumulate
  for days instead of resetting on every sleep. **Recommendation:** repair the waker first, and turn off
  sleeping only after the memory fixes ship.

## 2. Provider utilization (frozen 2026-10-13 read and code)

### 2.1 API-Football budget (CONFIRMED)

- Quota state: daily limit 7,500, operational cap 4,500. About 40 used by 03:33Z on 10-13, and about 228
  a day on 10-14.
- **The quota is not the constraint.** The constraints are wake frequency and the per-cycle caps.
- Cached request keys refreshed in the 7 days to 10-13:

| Endpoint | Keys refreshed in 7 d | Shape |
|---|---|---|
| `fixtures?date&league&season` | 80 | 5 leagues (39/40/41/42/140) × the UTC dates covering each Manila local day, 8 h TTL |
| `fixtures?id` | 24 | result-sync status track |
| `fixtures/statistics?fixture` | 32 | corner truth, 1 per fixture |
| `odds?fixture` | 43 | market collection, all bookmakers and bets in one call, 2 h TTL |

- **Wasted schedule calls (CONFIRMED):** in the last two wakes, **28 of 45** date requests returned an
  empty list (173 bytes). These are future dates with no fixtures in that league, such as the international
  break, and they are re-polled on every wake.
  - About 25 calls per wake are cheap against quota but pure waste.
  - Fix: a longer TTL for empty responses on dates more than 48 h ahead (low priority).

### 2.2 Why 10-10 results are missing: root cause (CONFIRMED)

The 9 fixtures at 10-10 14:00Z with 18 pending picks have `updated_at = 2026-10-10T13:40Z` (PRE_MATCH,
no score). Three things combine:

1. **The schedule sweep skips past local days.**
   - `sync_once` refreshes `today_local` plus the local dates of open **paper** rows
     (`services._scheduled_refresh_dates`, line 3415).
   - 14:00Z is 22:00 Asia/Manila. From the next wake at 18:34Z, local today was already 10-11, and there
     were no open paper rows for 10-10. The legacy paper lane has been idle since 09-14.
   - So no date sweep ever re-read 10-10 local.
   - The UTC `date=2026-10-10` request *was* re-fetched on 10-11 at 15:39Z, but only for local 10-11. The
     local-date filter (`fixture_provider.py:1343`) discards the 14:00Z rows.
   - **Open Model Pick dates are not in the refresh set.**
2. **Result-sync throughput is far below one weekend's volume.**
   - It runs once per wake with `SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE` at its default of 2: 1
     status slot plus 1 corner slot while both queues are non-empty (`result_sync.plan_result_sync_slots`).
   - At about 5 wakes a day, that is about 5 status fetches a day against **about 41 pick fixtures on
     10-09/10**.
   - `result_sync_attempts` confirms it: none of the 9 stuck fixtures was ever attempted.
3. **The 72 h lookback** (`RECENT_RESULT_LOOKBACK_HOURS`) expired on 10-13 at 14:00Z. Those fixtures then
   left the queue permanently.

**Same mechanism for corners (CONFIRMED):**
- 127 of 290 FINISHED API-Football fixtures have no corner result, and only 1 of those 127 has ever had a
  `fixtures/statistics` request.
- Corner truth is requested only for fixtures with a `corner_total` Model Pick (`needs_corner_truth_sync`)
  or an open paper corner row, at 1 slot per wake.
- 59 corner picks on FINISHED fixtures are waiting.

**Fix, already available in #111 (inside #122) and needing only flags:**

| Variable | Default | Recommended | Effect |
|---|---|---|---|
| `SPORTS_AI_RESULT_SYNC_STATUS_BATCH_SIZE` | 1 | **20** | one `fixtures?ids=a-…-t` request settles up to 20 statuses per slot |
| `SPORTS_AI_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS` | 0 | **14** | brings the expired 10-10 backlog back (hard max 60) |
| `SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE` | 2 | **4–6** | more corner-statistics slots per wake (hard max 10) |

**Cost:**
- With batch 20, the whole status backlog (about 75 non-final past-kickoff fixtures, 45 of them PRE_MATCH)
  clears in about 4 requests.
- The 59 corner fixtures need 59 statistics calls, about 1 % of the daily cap.

**Remaining code gap (SUSPECTED value, not implemented):** add the local dates of open LIVE_ISSUED Model
Picks to `_scheduled_refresh_dates`. That costs 5–10 requests per date and refreshes every fixture on that
day, including ones without picks.

### 2.3 Odds collection (CONFIRMED)

- `select_collection_fixtures` (`market_collection_policy.py:110`) only considers fixtures with a
  LIVE_ISSUED pick. Issuance last ran on 10-06, so 1 fixture qualified (`market_collection_selected_last_cycle`).
  That fixture returned 122 AH rows and 0 corner rows, as a CACHE_HIT with `CYCLE_MISSED` health.
- One `odds?fixture` call returns every bookmaker and every bet, including "Corners Over Under"
  (`market_data/api_football.py:62`). Corners therefore need no extra calls: a corner gap is either a
  timing gap (bookmakers publish corners later) or a selection gap, never a request gap.
- Fixtures picked from 10-07 (n=43): AH 43/43 and corners 42/43 at some point, but **only 1 of 43 had a
  capture within 60 minutes of kickoff**. The median last capture was about 34 h before kickoff.

**Fixes, in order:**
1. Repair the waker (§1.4).
2. Turn on the #110 issuance flag, so picked fixtures exist and get selected.
3. Turn on #113's `SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES` (about 90).
4. Code, behind a new default-OFF flag: let `PREDICTION_READY` fixtures with no pick enter collection at
   low priority. That gives an unbiased odds history for every predicted fixture, at about 1 call per
   fixture per 2 h TTL.

### 2.4 5Dollar shadow (CONFIRMED; details in #126)

- About 20 calls a day, FRA1 only, Bet365 only.
- Corners 9 of 18, because collection never rotated off the first two fixtures. #126 fixes that at the
  same call volume.
- **SUSPECTED:** the provider ignores the 48 h lookahead window.
- 0 fixtures overlap with API-Football, so 5Dollar cannot validate API-Football prices.
- No new provider should be added until the gaps in §2.2 and §2.3 are closed and measured again.

## 3. Ranked recommendations (no new providers, inside the current budget)

| # | Change | Kind | Value | Cost / risk |
|---|---|---|---|---|
| 1 | Redeploy `football-hourly-wake-v2` with its start command; delete v1 | Railway console | about 5 → 24 wakes a day; closing window reachable | none in budget terms; about 1,100 calls a day estimated |
| 2 | `RESULT_SYNC_STATUS_BATCH_SIZE=20`, `BACKLOG_LOOKBACK_DAYS=14`, `MAX_FIXTURES_PER_CYCLE=4` | env, after #122 deploy | settles the 20 overdue Oct 10–11 picks and the 59 corner picks | about 70 one-off calls |
| 3 | `SPORTS_AI_ENABLE_MODEL_PICK_ISSUANCE_SCHEDULER=1` | env, after backup and #122 | forward sample grows; odds collection has targets | none in calls |
| 4 | `MARKET_COLLECTION_CLOSING_WINDOW_MINUTES=90` | env | genuine closing prices for CLV | about 1 call per pick fixture |
| 5 | Add open-pick local dates to the schedule sweep | code (small) | bulk status refresh for whole match days | 5–10 calls per date |
| 6 | Optional low-priority collection of `PREDICTION_READY` fixtures | code + new flag (OFF) | unbiased price history | about 1 call per fixture per 2 h |
| 7 | Longer TTL for empty future-date schedule responses | code | saves about 25 calls per wake | negligible risk |
| 8 | Disable sleeping | Railway console | continuous loops | memory cost; only after the memory fixes |
