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
| monitoring | `GET/HEAD /api/monitoring/football`, also on the read-only token allowlist. | Endpoint live once deployed; token path closed until `SPORTS_EDGE_CLIENT_API_TOKEN` is set |

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

These ship with the code. Each one has been disclosed before and is listed here so approval is explicit:

1. **#120 `MODEL_STATE_NOT_BEFORE_KICKOFF`** (always on, fail-closed). A prediction is BLOCKED when the model state's latest research day is not strictly before the kickoff date. The 2026-10-12 production snapshot had 0 such predictions, so no READY prediction is expected to change.
2. **#109:** the paper headline drops to 820 counted positions (791 legacy AH quarantined).
3. **#110 / #118:** the Model Pick headline is forward-only. Retrospective rows are disclosed, and the display shows push-excluded q and stake-weighted win share.
4. **#110:** CLI `--apply-live-issued` refuses fixtures at or after kickoff.
5. **New:** `/api/monitoring/football` is served. It is session-protected like every other `/api/` route, and token-readable only once the token is configured.

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

- **Evidence for 2:** AH ECE was 0.067 with exactly one venue match, and 0.012 with two or more (`docs/FOOTBALL_POINT_IN_TIME_PARITY.md`).
- **What to validate first:**
  - Coverage: with `SPORTS_AI_FOOTBALL_MIN_VENUE_HISTORY_MATCHES=2`, rebuild predictions for the next 7 days against a read-only snapshot. Report READY to BLOCKED transitions by competition. Early-season promoted teams are the expected losses.
  - Behaviour: blockers `{HOME,AWAY}_MODEL_HISTORY_INSUFFICIENT` map to the existing match-filter statuses (pinned by `test_football_prediction_point_in_time.py`).
- **Ordering:** set it **before** issuance (#114 step 6). Issuance freezes the first pre-kickoff READY prediction, and a later change cannot unfreeze picks issued under 1.

## 6. Rollout order

This is #114 section 4, with this candidate's additions marked **new**.

| Step | Action | Approval |
|---|---|---|
| 0 | Backup (section 4) | – |
| 1 | Merge this candidate as **one** reviewed merge. The per-PR merges are inside it, so `git revert -m 1` of any single inner merge still works. Close #115. Mark #107–#114, #118 and #120 as included. | **merge approval** |
| 2 | Verify flags-OFF deploy: logs `state=disabled` for issuance, refresh and **high-confidence shadow** (new). `/api/monitoring/football` returns 200 when signed in and 401 when anonymous. | – |
| 3 | Generate `SPORTS_EDGE_CLIENT_API_TOKEN` and set it on Football and the Tennis host. Verify the 4 allowlisted paths with the token, 401 without it, and 401 on POST. | owner |
| 4 | App sleeping OFF (≈ $6–8/month gross, #114 §2) | **cost approval** |
| 5 | 24h baseline. `/api/monitoring/football` → `operational.alerts` reviewed. | – |
| 5a | **new:** min venue history = 2, after the section 5 coverage report | owner |
| 6 | Issuance ON, after the #114 model-freshness gate passes | owner |
| 7 | Prediction refresh ON | owner (provider budget) |
| 8 | Result-sync backlog settings | owner (provider budget) |
| 9 | Closing window 90 min | owner (provider budget) |
| 10 | **new:** high-confidence shadow ON. Writes only the shadow DB, publishes nothing. | owner |
| 11 | Retire wakers after ≥ 7 days stable | owner |

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
- [ ] Full CI-equivalent suite on the candidate. Results are in the PR description.
- [ ] GitHub `football-ci` green on the PR
- [ ] `/security-review` of the token allowlist change (new path `/api/monitoring/football`)
- [ ] Owner approval for section 3 items 1–5
