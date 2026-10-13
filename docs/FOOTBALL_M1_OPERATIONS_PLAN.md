# Football M1 production operations plan

Status: release-candidate proposal, revised 2026-10-13. Nothing in this
document has been applied to Railway, GitHub settings or production data.

Evidence sources:
- Railway service config, deploy logs and HTTP logs for deployment `cb32b656`
  (main `8f9b7e2`), 2026-10-09 to 2026-10-13;
- GitHub Actions run history;
- a read-only production snapshot taken 2026-10-12 15:22Z. It was taken
  `mode=ro` and streamed to stdout, with no writes inside the container;
- Railway's published pricing (docs.railway.com/reference/pricing/plans,
  read 2026-10-13);
- integration candidate `integration/football-m1-release-20261013` (draft #115,
  do not merge). It contains every PR below, merged onto main in release order.

## 1. Why production was awake only 4.4% of the time

| Service | Configuration | Observed behaviour |
|---|---|---|
| `sports-edge-ai` | Railway app sleeping on, 1 replica, volume `/data` | 21 cold starts in 3 days, each awake 6–10 min (one ~29 min), total 3h13m (4.4%). The container stops ~10 min after the last outbound traffic. |
| GitHub `football-production-wake.yml` | cron `17 * * * *`, `curl /health` | **The only effective waker.** GitHub ran it 30 times in ~7 days (≈4–5/day, gaps up to 9h) instead of 24/day. GitHub delays and drops scheduled runs. |
| `football-hourly-wake` | `curlimages/curl:8.16.0`, no cron | Broken since 2026-09-26. Its only run exited with `curl: try 'curl --help'`. It never sent a request. |
| `football-hourly-wake-v2` | `alpine:3.20`, cron `20 * * * *`, `wget … /health` | Its container starts every hour, but the primary's HTTP log shows **no request at :20–:24**. No effect. |

**Consequences.** Each wake runs the startup sync and about one cycle of each 600 s
loop. Nothing runs near kickoff, so:
- there are no closing odds;
- result sync and settlement lag by days: 204 open past-kickoff Model Picks;
- no Model Picks have been issued since 2026-10-06.

There is no duplicated scheduler execution: the waker services never reached the
primary, and the primary runs one scheduler child per container.

## 2. Cost and provider budget

**Railway (published rates, verified 2026-10-13).**

| Resource | Rate |
|---|---|
| RAM | $10 per GB-month |
| vCPU | $20 per vCPU-month |
| Volume | $0.15 per GB-month |
| Egress | $0.05 per GB |

The plan subscription includes usage credit: Hobby $5, Pro $20.

| Line | Always-on usage | Monthly |
|---|---|---|
| RAM | 0.55–0.75 GB average (supervisor 84 MiB + web 87 MiB + scheduler 350–560 MiB; HWM 991 MiB) | $5.50–7.50 |
| CPU | 0.02–0.05 vCPU average (loops are mostly idle) | $0.40–1.00 |
| Volume | unchanged | unchanged |
| **Gross always-on compute** | | **≈ $6–8.50** |

At today's 4.4% duty cycle the same service costs about $0.30–0.40/month, so the
increase is **≈ $6–8/month before any included credit**. Not verified from here:
- **the account's plan and remaining credit:** read them on the Railway usage page before approving;
- **whether a sleeping service accrues compute:** the pricing page doesn't say.

The two waker services keep their current small cost until they are retired in step 9.

**API-Football, always-on with every M1 flag enabled.** These are hard ceilings from the code:

| Source | Ceiling | Expected |
|---|---|---|
| Market collection: 144 cycles × ≤ 2 fixtures (`DEFAULT_COLLECTION_ONLY_MAX_FIXTURES`); the closing window only reorders inside that cap | ≤ 288/day | 100–250 (2h odds cache far from kickoff) |
| Result sync: 144 cycles × ≤ 6 requests (`SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE=6`) | ≤ 864/day | 50–250 (only while fixtures await truth) |
| Prediction refresh: 24 syncs × ~30 schedule/fixture requests | ≤ 720/day | 300–600 (1h/2h/8h schedule caches) |
| Model Pick issuance, settlement, value evaluation | 0 | 0 (DB-only) |
| **Total** | **≤ 1,872/day (42% of the 4,500 operational cap)** | **≈ 450–1,100/day** |

**Fail-closed limits** (unchanged unless noted):
- The effective daily allowance is min(provider limit 7,500, `SPORTS_AI_API_FOOTBALL_OPERATIONAL_DAILY_CAP`).
- The governor blocks LOW-priority requests in UNKNOWN or CONSERVE (< 40% left), allows only ESSENTIAL in CRITICAL (< 10%), and blocks everything in EXHAUSTED or with an open circuit.
- **New in #111:** result-sync backlog recovery runs only while the quota is HEALTHY. Otherwise it pauses (`result_sync_backlog_paused_reason=QUOTA_<state>`).

**Quota alerts (new in #111).** One log line per state transition:

```
provider quota alert: level=WARNING state=CONSERVE previous=HEALTHY effective_remaining=… effective_limit=…   (WARNING)
provider quota alert: level=CRITICAL state=CRITICAL previous=LOW …                                            (ERROR)
provider quota recovered: state=HEALTHY previous=…                                                            (INFO)
```

`/health` exposes `provider_budget_quota_state` and `provider_budget_quota_alert`
(`NONE` | `WARNING` | `CRITICAL`). A Railway log alert or an external monitor
should page on `provider quota alert: level=CRITICAL`.

## 3. Feature flags

Every flag below is off or neutral by default. Each is enabled only in its step
of section 4, and only after explicit approval.

| Variable | Default | Target | PR | Step |
|---|---|---|---|---|
| `SPORTS_AI_ENABLE_MODEL_PICK_ISSUANCE_SCHEDULER` | `0` | `1` | #110 | 6 |
| `SPORTS_AI_ENABLE_PREDICTION_REFRESH_SCHEDULER` | `0` | `1` (interval 3600) | #110 | 7 |
| `SPORTS_AI_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS` | `0` | `45` | #111 | 8 |
| `SPORTS_AI_RESULT_SYNC_STATUS_BATCH_SIZE` | `0` (1) | `20` | #111 | 8 |
| `SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE` | current | `6` | #111 | 8 |
| `SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES` | `0` | `90` | #113 | 9 |
| `SPORTS_AI_CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF` | `60` | `60` | #113 | – |
| `SPORTS_AI_FOOTBALL_MODEL_FRESHNESS_MODE` | `calendar` | leave `calendar` (policy decision) | #112 | – |
| `SPORTS_EDGE_CLIENT_API_TOKEN` | unset (routes closed) | `secrets.token_urlsafe(32)` on Football **and** the unified client | #107 | 2 |

These stay unchanged:
- `SPORTS_AI_ENABLE_AUTOMATED_SYNC=0`: the global scheduler would duplicate the market collector's requests;
- Value Bet issuance and publication: not set;
- `MARKET_COLLECTION_ONLY=1`;
- paid-provider settings and all quotas.

## 4. Staged deployment plan

Each code step works like this: merge → Railway auto-deploys main → verify → next
step. App sleeping stays **on** until step 4, so each deploy is one cold start.

| Step | Change | Expected behaviour | Verify (expected log / field) |
|---|---|---|---|
| 0 | Railway: volume backup of `/data` | – | Backup listed with timestamp; record the 4 DB file sizes (read-only `ls -l /data`). |
| 1 | Merge **#107** | HEAD served on 3 client routes | Section 5 auth checks. `/health` 200. |
| 2 | Merge **#109** | **Client-visible:** the paper headline drops to 811 counted positions | `/api/performance` → `paper_market_evidence_quarantine.market_evidence_quarantined_positions == 791`, `disputed_result_quarantined_positions == 9` |
| 3 | Merge **#110**, **#111**, **#113**, **#112**, **#114**, one at a time | Flags off. **Client-visible:** the Model Pick headline becomes forward-only (86 issued, 488 disclosed as retrospective). CLI issuance becomes forward-only. Collection attempt recency persisted. 3 aliases resolve. | Logs: `model pick issuance scheduler: state=disabled`, `prediction refresh scheduler: state=disabled`. `/health`: `model_pick_issuance_*`, `result_sync_backlog_lookback_days: 0`, `provider_budget_quota_alert`. No `Traceback`; no `sqlite3.OperationalError`. |
| 4 | **Approval:** Railway app sleeping **off**. Wakers untouched. | Continuous loops | No `Stopping Container` for 24h. `market collection scheduler started: interval_seconds=600` appears exactly once per deploy. |
| 5 | Observe 24h | Baseline provider usage | `/health.provider_budget` daily usage below 300 (no new loops yet); `provider_budget_quota_alert: NONE`. |
| 6 | Env: issuance **on** (DB-only) | Forward picks for READY fixtures with kickoff ahead | `model pick issuance scheduler started: interval_seconds=600`, then `model pick issuance: written=N ah=… corners=…`. Every new ledger row has `created_at < kickoff_utc`. A second cycle logs `written=0`. |
| 7 | Env: prediction refresh **on** | Hourly fixture and prediction refresh, no market sync | `prediction refresh scheduler started: interval_seconds=3600`; hourly `fixture sync started: kind=scheduled_prediction_refresh include_schedule=True`. Daily provider usage within the section 2 ceiling. |
| 8 | Env: result-sync backlog settings | The backlog drains while quota is HEALTHY | `/health` `result_sync_backlog_selected_last_cycle` non-empty, `result_sync_backlog_paused_reason: null`. Open past-kickoff picks fall from 204 toward the blocked 73 (67 identity, 6 postponed or cancelled). |
| 9 | Env: closing window `90` | Near-kickoff capture | Snapshots with `captured_at` within 60 min of kickoff for Model Pick fixtures; `closing_captured_at` populated on new paper positions. |
| 10 | After ≥ 7 days of steps 4–9 stable: retire wakers | – | Delete `football-hourly-wake` and `football-hourly-wake-v2`. Change `football-production-wake.yml` to a 3-hourly health check that fails loudly. |

Expected first issuance (dry run of #110 against the 15:22Z snapshot): **12 picks
for 6 fixtures** (ENG3 1, ENG4 5). 12 fixtures are withheld because their latest
prediction is not READY. Fixtures that kicked off since the snapshot drop out
automatically.

## 5. Verification commands

None of these print secret values.

```sh
# deployment + sleep state
railway status --json | python -c "import json,sys;d=json.load(sys.stdin);e=[x for x in d['environments']['edges'] if x['node']['name']=='production'][0]['node'];[print(s['node']['serviceName'],s['node']['latestDeployment']['status']) for s in e['serviceInstances']['edges']]"
railway logs -s sports-edge-ai -n 400 | grep -E "Starting Container|Stopping Container|scheduler started|state=disabled|model pick issuance|fixture sync started|provider quota|Traceback"
curl -s https://sports-edge-ai-production.up.railway.app/health | python -m json.tool | grep -E "model_pick_issuance|prediction_refresh|result_sync|market_collection|provider_budget_quota"
# read-only token (value read from your own secret store, never echoed)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" https://sports-edge-ai-production.up.railway.app/api/system/status   # 200
curl -s -o /dev/null -w "%{http_code}\n" https://sports-edge-ai-production.up.railway.app/api/system/status                                    # 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Authorization: Bearer $TOKEN" https://sports-edge-ai-production.up.railway.app/api/fixtures/refresh  # 401
# Model Pick backlog + duplicate identities (read-only; run against a ro snapshot)
python -m sports_ai.diagnostics.football_model_pick_backlog --model-pick-db <snapshot> --dashboard-db <snapshot>
```

## 6. Rollback

| Change | Rollback |
|---|---|
| Any env flag | Unset it. The loop logs `state=disabled` on the next deploy. Picks and results already written are valid and never modified. |
| App sleeping off | Turn it back on. The wakers are still present until step 10. |
| Waker retirement (step 10) | Recreate from section 1, or re-enable the GitHub workflow cron. |
| Any PR | `git revert -m 1 <merge sha>` on main; Railway redeploys. Schema changes are additive (new runtime-state keys, read-time fields only), so the previous code reads the same DBs. |
| Data | Restore the step-0 volume backup. Only for corruption, never to "undo" valid forward records. |

## 7. Open decisions for the owner

1. Approve app sleeping off (≈ $6–8/month before plan credit). Wakers are retired only at step 10.
2. Approve the client-visible headline changes:
   - #109: paper 811 counted; 791 legacy AH and 9 disputed quarantined;
   - #110: Model Pick headline forward-only.
3. Osasuna v Getafe (ESP1, 2026-08-31): football-data.org 1-1, API-Football 1-0.
   - Verify against an official source.
   - Then remove the entry from `DISPUTED_FINAL_SCORE_FIXTURES` and decide whether a documented correction record is needed. Ledger rows are never rewritten in place.
4. Whether to adopt `SPORTS_AI_FOOTBALL_MODEL_FRESHNESS_MODE=schedule`. It is stricter, and READY fixtures drop to 0 during weekend publication lag.
5. How to treat the 67 open retrospective picks on football-data.org fixture ids that API-Football cannot poll: identity mapping or retirement.
6. A VOID state for the 6 postponed or cancelled picks.
