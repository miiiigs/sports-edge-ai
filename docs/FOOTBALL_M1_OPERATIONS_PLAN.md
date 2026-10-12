# Football M1 production operations plan

Status: proposal, 2026-10-12. Nothing in this document has been applied to
Railway, GitHub settings or production data.

Evidence sources:
- Railway service config, deploy logs and HTTP logs for deployment `cb32b656`
  (main `8f9b7e2`), 2026-10-09 to 2026-10-12;
- GitHub Actions run history;
- a read-only production snapshot taken 2026-10-12 15:22Z. It was taken
  `mode=ro` and streamed to stdout, with no writes inside the container.

## 1. Why production was awake only 4.4% of the time

| Service | Configuration | Observed behaviour |
|---|---|---|
| `sports-edge-ai` | Railway app sleeping on, 1 replica, volume `/data` | 21 cold starts in 3 days, each awake 6–10 min (one ~29 min), total 3h13m (4.4%). The container stops ~10 min after the last outbound traffic. |
| GitHub `football-production-wake.yml` | cron `17 * * * *`, `curl /health` | **The only effective waker.** GitHub ran it 30 times in ~7 days (≈4–5/day, gaps up to 9h) instead of 24/day. GitHub delays and drops scheduled runs. Every primary wake lines up with one of these runs, from Azure source IPs. |
| `football-hourly-wake` | `curlimages/curl:8.16.0`, no cron, startCommand starting `--fail …` | Broken since 2026-09-26. Its only run exited with `curl: try 'curl --help'`. It never sent a request. |
| `football-hourly-wake-v2` | `alpine:3.20`, cron `20 * * * *`, `sh -ec 'wget … /health'` | Its container starts every hour, but the primary's HTTP log shows **no request at :20–:24**, and its own `wake-start` echo never appears in its logs. It has no effect, and it costs an hourly container start. |

**Consequences.** Each wake runs:
- the startup sync: research refresh, fixtures, predictions;
- about one cycle of each 600s loop: market collection, result sync, Model Pick settlement, value evaluation.

Nothing runs near kickoff. As a result:
- there are no closing odds: the last pre-match capture for 10-10 fixtures was 414–2713 min before kickoff;
- result sync and settlement lag by days: 204 open past-kickoff Model Picks;
- the research refresh only happens on a wake.

There is no duplicated scheduler execution: the waker services never reached the
primary, and the primary runs one scheduler child per container.

## 2. Options

| Option | Reliability | Railway cost (estimate) | Notes |
|---|---|---|---|
| A. Keep sleeping, fix one Railway cron waker every 15 min | Medium: ≥96 starts/day, each a cold start with research refresh and model build (scheduler HWM ≈ 1 GB) | ~50–70% of always-on, because each wake stays up ~10 min | Cold-start churn, and still no guaranteed near-kickoff capture between wakes. |
| **B. Disable app sleeping on `sports-edge-ai` (recommended)** | High: continuous loops, closing-window capture possible, timely settlement | RAM ≈ 0.55–0.75 GB average (supervisor 84 MiB + web 87 MiB + scheduler 350–560 MiB, HWM 991 MiB) | One process tree, no cold-start churn. |
| C. Separate scheduled worker service | Not viable now | – | A Railway volume mounts to one service only, and the ledgers are SQLite on that volume. A second service cannot share them without a database migration. |

**Cost estimate for B.** These are Railway list prices at the time of writing;
check them on the account's usage page before deciding.

| Line | Rate | Always-on cost |
|---|---|---|
| RAM | ≈ $10 / GB-month | ≈ 0.65 GB → **≈ $6.5/month** |
| CPU | ≈ $20 / vCPU-month | Mostly idle; 0.02–0.05 vCPU average → **≈ $0.5–1/month** |
| Volume | 2 GB | Unchanged |

**Net increase ≈ $7–9/month** compared with today's ≈ 4.4% duty cycle. Removing the
two waker services saves their hourly container starts.

**Provider budget.** API-Football is observed at 165–270 requests/day today,
against an effective cap of 4,500/day and a daily limit of 7,500. Always-on with
the proposed loops:

| Source | Requests/day |
|---|---|
| Market collection (2 fixtures × 144 cycles, upper bound) | ≤ 288 |
| Result sync (6 per cycle, upper bound) | ≤ 864 |
| Prediction refresh (hourly, ~30 per sync) | ≈ 720 |
| Closing window (estimate) | 20–100 |
| **Total** | **≈ 1.2k–2.0k** |

The quota governor, request cache, rate limiter and circuit breaker are
unchanged and remain authoritative.

## 3. Recommended configuration (needs approval)

Railway, service `sports-edge-ai`:
1. Settings → Deploy → **Serverless / App sleeping: off**. Keep 1 replica, region `ams`.
2. Keep `SPORTS_AI_ENABLE_AUTOMATED_SYNC=0`. The global scheduler would duplicate
   the market collector's requests; the dedicated loops below replace it.

Railway, wakers:
3. Delete `football-hourly-wake` (broken).
4. Delete `football-hourly-wake-v2` (ineffective).

GitHub:
5. Change `football-production-wake.yml` to a health check that fails loudly:
   keep `workflow_dispatch` and move the cron to every 3 hours. Alternatively,
   disable the workflow once sleeping is off.

Environment, applied only after the matching PRs are merged and deployed:

| Variable | Value | PR |
|---|---|---|
| `SPORTS_AI_ENABLE_MODEL_PICK_ISSUANCE_SCHEDULER` | `1` | #110 |
| `SPORTS_AI_ENABLE_PREDICTION_REFRESH_SCHEDULER` | `1` (interval default 3600) | #110 |
| `SPORTS_AI_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS` | `45` | #111 |
| `SPORTS_AI_RESULT_SYNC_STATUS_BATCH_SIZE` | `20` | #111 |
| `SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE` | `6` | #111 |
| `SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES` | `90` | #113 |
| `SPORTS_AI_CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF` | `60` (default) | #113 |
| `SPORTS_AI_FOOTBALL_MODEL_FRESHNESS_MODE` | leave `calendar` (policy decision) | #112 |
| `SPORTS_EDGE_CLIENT_API_TOKEN` | `secrets.token_urlsafe(32)` on Football **and** the unified client | #107 |

These stay unchanged: Value Bet issuance (`SPORTS_AI_ENABLE_VALUE_BET_ISSUANCE`
is not set), paid-provider settings, `MARKET_COLLECTION_ONLY=1`, and all quotas.

## 4. Controlled deployment plan

Each step: merge → Railway auto-deploys main → verify → next step. Every new
flag defaults off, so each merge on its own is behaviour-neutral, except for the
noted default changes.

| Step | Change | Default behaviour change | Verify |
|---|---|---|---|
| 0 | Railway: snapshot `/data` (Railway volume backup) | – | Backup listed |
| 1 | Merge #107 (read-only client token) | HEAD served on 3 routes | §5 auth checks |
| 2 | Merge #109 (legacy AH quarantine) | **Client-visible:** AH paper headline 791 → 0 counted, 791 quarantined; overall paper headline becomes the 820 non-AH rows | `/api/performance` → `paper_market_evidence_quarantine.market_evidence_quarantined_positions == 791` |
| 3 | Merge #110 (forward issuance + prediction refresh) | CLI issuance becomes forward-only | `/health` exposes `model_pick_issuance_*` |
| 4 | Merge #111 (result-sync backlog) | Non-API-Football ids no longer take status slots | `/health` `result_sync_*` |
| 5 | Merge #113 (odds capture + CLV closing) | Attempt recency persisted; CLV closing restricted to ≤60 min before kickoff and trusted AH versions | `market_runtime_state.market_collection_attempts` present |
| 6 | Merge #112 (aliases; freshness flag off) | 3 aliases resolve | Mansfield / Forest / Stoke mappings resolved |
| 7 | Railway: app sleeping off; delete both waker services | Continuous loops | No `Stopping Container` for 24h |
| 8 | Env: issuance + prediction refresh on | Forward picks issued | `model_pick_issuance_written_last_cycle`; ledger rows with `created_at < kickoff_utc` |
| 9 | Env: result-sync backlog settings | Backlog drains | Open past-kickoff picks fall from 204 |
| 10 | Env: closing window 90 | Near-kickoff capture | `closing_captured_at` within 60 min of kickoff |

Expected first issuance, from a dry run of #110 against the 15:22Z snapshot copy:
**12 picks for 6 fixtures** (ENG3 1, ENG4 5). 12 fixtures are withheld because
their latest prediction is not READY.

## 5. Verification commands

None of these print secret values.

```sh
# deployment + sleep state
railway status --json | python -c "import json,sys;d=json.load(sys.stdin);e=[x for x in d['environments']['edges'] if x['node']['name']=='production'][0]['node'];[print(s['node']['serviceName'],s['node']['latestDeployment']['status'],s['node'].get('sleepApplication')) for s in e['serviceInstances']['edges']]"
railway logs -s sports-edge-ai -n 400 | grep -E "Starting Container|Stopping Container|scheduler started|model pick issuance|result sync|market sync"
curl -s https://sports-edge-ai-production.up.railway.app/health | python -m json.tool | grep -E "model_pick_issuance|prediction_refresh|result_sync|market_collection|scheduler"
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
| App sleeping off | Turn it back on. If hourly wakes are wanted again, re-enable the GitHub workflow cron. |
| Waker services deleted | Recreate from this table if needed. Neither was functional. |
| Any env flag | Unset it. Loops become no-ops, and already-written picks and results are never modified. |
| Any PR | `git revert` the merge commit on main; Railway redeploys. Ledger rows written while the change was active stay valid: picks are forward-only, settlements come only from verified results. |
| Data | Restore the step-0 volume backup. Only for corruption, never to "undo" valid forward records. |

## 7. Open decisions for the owner

1. Approve the Railway change (sleep off, ≈ $7–9/month) and the deletion of both waker services.
2. Approve the client-visible headline change from #109.
3. Decide whether Model Pick headline `summary` should become forward-only. #110
   discloses `by_issuance_timing` and `forward_by_market` but leaves `summary` unchanged.
4. Osasuna v Getafe (ESP1, 2026-08-31) has conflicting final scores between
   providers: football-data.org 1-1, API-Football 1-0. Verify against an
   official source before any correction.
5. Whether to adopt `SPORTS_AI_FOOTBALL_MODEL_FRESHNESS_MODE=schedule`. It is
   stricter, and READY fixtures drop to 0 during weekend publication lag.
6. How to treat the 40 open picks on football-data.org fixture ids that
   API-Football cannot poll: identity mapping or retirement.
