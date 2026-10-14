# Football M1 release candidate (supersedes #115)

Status: **code-only release candidate, 2026-10-13.** Nothing here has been merged
to main, deployed, or applied to Railway variables or production data. Every new
runtime behaviour is behind a flag that defaults OFF. The one exception is the
fail-closed safeguard in #120, described in section 3.

Branch: `integration/football-m1-rc-20261013`, built on main `6c67200`
(the deployed SHA, verified on Railway 2026-10-13).

Companion documents:
- `docs/FOOTBALL_M1_OPERATIONS_PLAN.md` (#114): wake/sleep evidence, cost,
  provider budget, verification commands. Still authoritative for those topics.
- `docs/FOOTBALL_MONITORING_API.md`: the read-only monitoring contract for the
  unified dashboard.
- `docs/FOOTBALL_HIGH_CONFIDENCE_SHADOW.md`: shadow lane protocol and gates.
- `docs/FOOTBALL_M1_ACTIVATION_READINESS.md`: min-history coverage, model
  freshness, scheduler/sleep requirements and the result backlog inventory
  (2026-10-14).

## 1. Why a new candidate instead of #115

#115 merged #107, #109, #110, #111, #112, #113 and #114 onto `8f9b7e2`, i.e.
before #116, #117, #119 and #121 landed. It has no unique commits; its only
conflict resolution was an import block in `services.py`. It also lacks #118 and
#120. **#115 should be closed, not merged.**

This candidate merges the same branch heads onto current main, plus #118, #120
and #108, in an order that produces no conflicts (#111 before #110). Compared
with #115, within every file #115 touches, the only differences are the #118
and #120 deltas.

| Order | PR | Branch head | Merge result | Already in main? |
|---|---|---|---|---|
| 1 | #107 read-only client token | `a9d203a` | clean | no |
| 2 | #109 legacy AH paper quarantine | `f9cfd83` | clean | no |
| 3 | #111 result-sync backlog | `8885c53` | clean | no |
| 4 | #112 team aliases + freshness | `bd29652` | clean | no |
| 5 | #113 odds recency + closing window | `d61312a` | clean | no |
| 6 | #110 + #118 forward issuance + probability display | `4dbca94` (contains #110 `7970749`) | clean | no |
| 7 | #120 pre-kickoff guard + min history | `fce0f61` (parent #119 is in main) | clean | no |
| 8 | #114 operations plan (docs) | `c8c40a1` | clean | no |
| 9 | #108 weekend report | `de7577c` | clean | no |
| – | #116, #117, #119, #121 | – | – | **yes**, not duplicated |

Commits added on top of the merges:

| Commit | Change | Default |
|---|---|---|
| weekend report | Counts only picks with `created_at < kickoff`, not just `record_origin = LIVE_ISSUED`. Early CLI runs labelled kicked-off fixtures LIVE_ISSUED. | n/a (read-only CLI) |
| shadow hook | The worker runs the #121 lane on the issuance cadence. | OFF (`SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW`) |
| monitoring | `GET/HEAD /api/monitoring/football`, also on the read-only token allowlist. Owner session or token only; guests get 403. 60 s cache. | Endpoint live once deployed; token path closed until `SPORTS_EDGE_CLIENT_API_TOKEN` is set |
| final QA (2026-10-14) | Issuance re-checks `model_pick_id` and the canonical holder under `BEGIN IMMEDIATE`. Same-process overlapping cycles are skipped. CLI `--apply-live-issued` skips matches already held under another provider id. The closing-quote window uses `last_observed_at`. Adds the unified-card adapter contract plus golden files. | n/a (safety fixes) |

## 2. Feature flags in this candidate

Everything in the #114 plan's section 3 still applies. Additions:

| Variable | Default | Proposed | Source | Gate |
|---|---|---|---|---|
| `SPORTS_AI_FOOTBALL_MIN_VENUE_HISTORY_MATCHES` | `1` (current behaviour) | `2`, after coverage check (section 5) | #120 | owner approval |
| `SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW` | unset (OFF) | `1` after M1 steps 4–7 are stable | #121 + hook | owner approval |
| `SPORTS_AI_HIGH_CONFIDENCE_SHADOW_DB_PATH` | next to the Model Pick DB | leave default | #121 | – |
| `SPORTS_AI_FOOTBALL_CORNER_PROBABILITY_TEMPERATURE` | OFF | leave OFF pending independent validation | #117 | – |

These stay unset: Value Bet issuance, the lifecycle scheduler, publication, real-money betting, and any staking change.

## 3. Behaviour that changes on deploy with every flag OFF

These ship with the code. None of them is zero-behaviour, and each needs explicit approval. The independent review on 2026-10-14 verified the list:

1. **#120 `MODEL_STATE_NOT_BEFORE_KICKOFF`** (always on, fail-closed). A prediction is BLOCKED when the model state's latest research day is not strictly before the kickoff date. The 2026-10-12 and 2026-10-13 snapshots had 0 such predictions.
2. **#109:** the paper headline drops to 820 counted positions, with 791 legacy AH positions quarantined. This applies on every reporting surface, including fixture detail, and adds new `market_evidence_quarantine*` keys.
3. **#110 / #118:** the Model Pick headline, `by_market` and daily history are forward-only. The UI label becomes "Forward Model Picks". The probability column shows push-excluded q (1/fair) in place of `model_probability`.
4. **#110:** CLI `--apply-live-issued` refuses fixtures at or after kickoff, and fixtures whose latest prediction is not READY. **New:** it also skips matches already held under another provider id. A `--expected-manifest-hash` can go stale if a fixture kicks off between dry-run and apply.
5. **#113 CLV closing quote** (always on, `SPORTS_AI_CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF` = 60). For positions whose `closing_odds` is still null, the quote must have been observed within 60 min before kickoff and be value-eligible; there is no fallback to older quotes. Stored values are never rewritten. **New:** "observed" uses `last_observed_at`. Without that, a price that stayed unchanged into the window (de-duplicated) left CLV null forever.
6. **#113 collection-only mode:** attempts are recorded and change the rotation order. They are exposed in `/api/system/status`.
7. **#111 result sync:** non-API-Football fixture ids are counted as `provider_mismatch` and no longer take a poll slot. Their provider already returned nothing for them, so no result changes.
8. **API:**
   - New `/api/monitoring/football` (owner session or token; guests get 403).
   - HEAD is now served on `/api/candidates`, `/api/performance` and `/api/system/status`.
   - `/api/system/status` and `/health` gain issuance, refresh, shadow, quota-alert and result-sync fields.
   - A quota alert is logged on each quota-state transition.
9. **Worker boot:** 3 new loops start, log "disabled" and exit. There are no new DB writes at boot.

## 4. Production datastore: backup and restore

Volume `sports-edge-ai-volume` is mounted at `/data`:

| File | Owner | Written by |
|---|---|---|
| `football_dashboard.sqlite` | fixtures, predictions, results, runtime state | web + scheduler |
| `football_paper_trading.sqlite` | market snapshots, paper/value positions | scheduler |
| `football_model_picks.sqlite` | Model Pick ledger (immutable fields, triggers) | issuance/settlement |
| `sports_edge_auth.sqlite` | users and sessions | web |
| `football_high_confidence_shadow.sqlite` | shadow lane (created only when enabled) | shadow hook |

**Backup (before step 1, and before each flag step).**
1. Take a Railway volume backup of `sports-edge-ai-volume` (Railway UI → service → Volume → Backups). Record its timestamp.
2. Take an independent logical copy that does not write inside the container. Run a `python -c` one-liner over `railway ssh`. For each DB it opens `file:/data/<db>?mode=ro`, calls `sqlite3.Connection.backup()` into an in-memory DB, then `iterdump()`s it gzip+base64 to stdout. Decode locally. This is the same pattern as the 2026-10-12 read-only snapshot. Store the files outside the repo.
3. Verify the copy locally. Run `PRAGMA integrity_check` on each file, then record row counts for `football_model_pick_entries`, `paper_trade_entries`, `paper_trade_updates`, `market_snapshots`, `dashboard_predictions` and `dashboard_fixtures`.

**Restore** (corruption only, never to "undo" valid forward records):
1. Turn issuance, refresh and shadow OFF, then redeploy so no loop writes.
2. Restore the Railway volume backup. Do not hand-copy partial files: the DBs are written together.
3. Redeploy, then confirm `/health` and the row counts.
4. Forward picks issued after the backup are lost by a restore. Re-issue never recreates them, because their fixtures have kicked off. Prefer `git revert` plus flag-off over restore.

## 5. Activating minimum venue history = 2 (not done)

**Coverage report done 2026-10-14:** `FOOTBALL_M1_ACTIVATION_READINESS.md` §1.
- 2 is weakly supported: the history-1 AH slice runs about 10 pt below q in both periods.
- 3 is rejected.
- 0 READY fixtures are lost in the current window; about 1% per season is lost, all in August.

- **Evidence for 2:** AH ECE was 0.067 with exactly one venue match, and 0.012 with two or more (`docs/FOOTBALL_POINT_IN_TIME_PARITY.md`).
- **What to validate first:**
  - Coverage: with `SPORTS_AI_FOOTBALL_MIN_VENUE_HISTORY_MATCHES=2`, rebuild predictions for the next 7 days against a read-only snapshot. Report READY to BLOCKED transitions by competition. Early-season promoted teams are the expected losses.
  - Behaviour: blockers `{HOME,AWAY}_MODEL_HISTORY_INSUFFICIENT` map to the existing match-filter statuses (pinned by `test_football_prediction_point_in_time.py`).
- **Ordering:** set it **before** issuance (#114 step 6). Issuance freezes the first pre-kickoff READY prediction, and a later change cannot unfreeze picks issued under 1.

## 6. Rollout order

This is the reviewed release sequence. Each step must pass its check before the next starts. Strong Value Bets, Value Bet issuance, corner calibration (#117), publication and real-money betting stay OFF throughout.

| # | Step | Exact action / check | Approval |
|---|---|---|---|
| 1 | Final PR #122 review | Independent review done 2026-10-14: no blocking defects. M1 (closing window), M2 (guest access, uncached scans) and L1 (overlap) were fixed in this branch. Remaining low items: L2 mixed `Z` / `+00:00` timestamp sort; L3 UTC vs UK date at 23:00–24:00 kickoffs. | – |
| 2 | CI and security | GitHub `football-ci` and GitGuardian green on the **final** head. The token allowlist is unchanged at 4 paths. Monitoring tests prove there is no error text or path in the payload, and that guests get 403. | – |
| 3 | `/data` backup and restore readiness | Section 4: Railway volume backup plus an independent `mode=ro` logical copy. `PRAGMA integrity_check` and row counts recorded locally **before** step 4. | owner runs the backup |
| 4 | Code-only merge and deploy | One merge of #122. Close #115, mark #107–#114, #118 and #120 as included. Railway auto-deploys **with every flag unset**. | **merge/deploy approval** |
| 5 | Production health and ledger checks | `/health`: issuance, refresh and shadow report `disabled`. `RAILWAY_GIT_COMMIT_SHA` equals the merge SHA. Row counts in all 4 DBs equal the step 3 counts (no writes from the deploy). Paper headline is 820. The Model Pick headline is forward-only. `/api/monitoring/football` returns 200 signed in, 401 anonymous, 403 guest. | – |
| 6 | Model freshness | `/api/monitoring/football` `operational` plus `model_snapshot_state`: every supported competition is FRESH, and `model_as_of` is ≥ the last completed matchday. 0 `MODEL_STATE_NOT_BEFORE_KICKOFF` blocks for upcoming fixtures. READY rate ≥ 80% of fixtures in the next 72 h. | – |
| 7 | Provider budget | Confirm the API-Football quota (7,500/day; 4,500 operational cap) against planned use: collection, refresh, about 120 one-off backlog requests, closing window. Set `SPORTS_AI_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS` and `…_STATUS_BATCH_SIZE` only with this approval. | **budget approval** |
| 8 | Always-awake cost | Railway app sleeping OFF (≈ $6–8/month gross). The alternative is in readiness §3. Check: no `Stopping Container` for 24 h, and each scheduler logs `started` exactly once. | **cost approval** |
| 9 | Controlled scheduler activation | In order, with 24 h between each: (a) optional `SPORTS_AI_FOOTBALL_MIN_VENUE_HISTORY_MATCHES=2` (readiness §1); (b) `SPORTS_AI_ENABLE_MODEL_PICK_ISSUANCE_SCHEDULER=1`; (c) `SPORTS_AI_ENABLE_PREDICTION_REFRESH_SCHEDULER=1`; (d) backlog settings; (e) `SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES=90`. | owner, per flag |
| 10 | Forward-issued prediction acceptance | Over the first full matchweek, every new pick has `created_at < kickoff_utc`. `excluded_from_headline.live_issued_not_forward` does not grow. `canonical_duplicates` and `conflicts` are 0. `READY_FIXTURES_WITHOUT_PICK` is not raised after a cycle. Volume is about 80–100 picks per week. | owner sign-off |
| 11 | Optional high-confidence shadow | `SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW=1` only after step 10 is accepted. It writes only the shadow DB and publishes nothing. Retire the wakers ≥ 7 days after step 8. | owner |

## 7. Rollback

| Change | Rollback |
|---|---|
| The whole candidate | `git revert -m 1 <candidate merge sha>`. Schema changes are additive (runtime-state keys, a separate shadow DB file), so `6c67200` reads the same DBs. |
| A single inner PR | `git revert -m 1 <inner merge sha>` on main |
| Min venue history | Unset (default 1). Predictions recompute on the next refresh. Picks already issued are unchanged. |
| Shadow lane | Unset the flag. The shadow DB is inert; leave it in place for the audit trail. |
| Monitoring endpoint | Remove the token path from `CLIENT_API_READ_PATHS` and redeploy, or revert the commit |
| App sleeping | Turn it back on (wakers still present until step 11) |

## 8. Pre-merge checklist

- [x] Clean merge of every M1 PR onto `6c67200`; no conflict hunks
- [x] Focused suites: issuance, lifecycle, backlog, token (incl. production composition), quarantine, PIT guard, shadow + hook, monitoring, weekend report
- [x] Full CI-equivalent suite on the candidate. Results are in the PR description.
- [x] GitHub `football-ci` green on the PR (re-check on the final head)
- [x] Independent review of the token allowlist and monitoring route (2026-10-14). Guests get 403; there is no error text or path leakage.
- [x] Min-history coverage report, model-freshness and backlog inventory (`FOOTBALL_M1_ACTIVATION_READINESS.md`)
- [ ] Owner approval for section 3 items 1–9
