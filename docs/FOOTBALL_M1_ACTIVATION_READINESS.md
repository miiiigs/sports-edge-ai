# Football M1 activation readiness (2026-10-14)

Companion to `FOOTBALL_M1_RELEASE_CANDIDATE.md`. All production figures come from
the **read-only snapshot taken 2026-10-13 03:34Z**. A newer snapshot could not be
taken: production was asleep, and this session was not permitted to read it.
Everything below is therefore **historical as of that instant**. No production
setting, data or variable was changed.

## 1. Minimum venue history (P0-B)

### 1a. Holdout replay

Method:
- Production balanced-line Model Pick policy (AH and corners).
- `football_day10_model_prices.jsonl` walk-forward prices.
- Venue history defined as `min(prior home matches of the home team, prior away matches of the away team)` in the same competition (`football_high_confidence_benchmark.venue_history`).
- Stake-weighted win share with pushes excluded; Wilson 95% CI; ECE over 10 q-bins.

Script: `minhist_holdout.py` (session scratchpad, read-only).

| Holdout 2024/25–2025/26 | min 1 (current) | min 2 | min 3 |
|---|---|---|---|
| AH picks | 4,784 | 4,738 | 4,693 |
| AH win share | 52.5% (51.0–54.0) | 52.6% (51.1–54.1) | 52.5% (51.0–54.1) |
| AH calibration gap / ECE | +0.4 pt / 0.005 | +0.5 pt / 0.005 | +0.4 pt / 0.005 |
| Corner picks | 4,784 | 4,738 | 4,693 |
| Corner win share | 50.6% | 50.6% | 50.5% |
| Corner calibration gap | −1.1 pt | −1.1 pt | −1.2 pt |

Only the excluded slice itself is informative:

| Slice removed | Period | AH n | AH win share | AH gap vs q | Corners win share |
|---|---|---|---|---|---|
| history = 1 (removed by min 2) | holdout | 46 | 44.6% (31–60) | **−7.5 pt** | 54.3% |
| history = 1 | selection 22/23–23/24 | 81 | 39.7% (29–52) | **−12.0 pt** | 52.0% |
| history = 2 (removed by min 3) | holdout | 45 | 60.3% (45–74) | +7.9 pt | 57.7% |
| history = 2 | selection | 78 | 54.0% (42–65) | +2.0 pt | 59.1% |

Per league, holdout fixtures removed by min 2 / min 3:

| League | min 2 | min 3 |
|---|---|---|
| ENG1 | 4 | 8 |
| ENG2 | 8 | 16 |
| ENG3 | 16 | 31 |
| ENG4 | 14 | 28 |
| ESP1 | 4 | 8 |

Across all leagues, min 2 removes 46 of 4,784 fixtures (~1%) over two seasons. **Every removed fixture is in August or early September.**

### 1b. Current production window (snapshot, kickoffs 2026-10-13 to 2026-10-21)

| League | Eligible | READY | READY at min 1 / 2 / 3 | Blocked (reason) |
|---|---|---|---|---|
| ENG1 | 5 | 4 | 4 / 4 / 4 | 1 with no prediction yet |
| ENG2 | 24 | 20 | 20 / 20 / 20 | 2 `TEAM_IDENTITY_UNRESOLVED` (Stoke City), 2 `MODEL_HISTORY_MISSING` (Lincoln, promoted) |
| ENG3 | 12 | 10 | 10 / 10 / 10 | 1 `TEAM_IDENTITY_UNRESOLVED` (Mansfield Town), 1 `MODEL_HISTORY_MISSING` (MK Dons, promoted) |
| ENG4 | 12 | 12 | 12 / 12 / 12 | – |
| ESP1 | 5 | 3 | 3 / 3 / 3 | 2 with no prediction yet |

- **Promoted-team impact of 2 or 3: none this window.** The two promoted teams that are blocked, Lincoln and MK Dons, are blocked as `MODEL_HISTORY_MISSING` (zero rows), which already blocks them at 1. Every READY fixture has at least 50 venue matches.
- The #112 aliases in this RC fix the three Stoke and Mansfield identity blocks, which adds about 3 READY fixtures.
- **Open finding:** Lincoln and MK Dons have about 4 venue matches each this season but still show zero model history. That points to a name mismatch between provider and research rows for promoted teams, not to the threshold. It needs its own alias audit.

### 1c. Verdict

- **Threshold 2: supported, weakly.**
  - The history = 1 AH slice underperforms its own q in both independent periods (pooled ≈ 41% vs ≈ 52% baseline, n = 127). That matches the earlier ECE finding (0.067 at one match vs 0.012 at two or more).
  - Headline accuracy and ECE barely move, because the slice is ~1% of volume.
  - It costs about 23 fixtures per season, all in the first 3–4 weeks of the season, and 0 now.
- **Threshold 3: rejected.** The history = 2 slice performs at or above baseline, and min 3 doubles the early-season losses.
- **Weekly volume reduction:** 0% for October–May. In August, roughly 10–15% of fixtures for 2–3 matchweeks.
- **Default unchanged at 1.** Setting 2 is RC step 5a and needs owner approval.

## 2. Model freshness (P0-C)

Taken from `model_snapshot_state` in the snapshot. Model state is rebuilt from the research store at startup sync.

| Competition | Latest completed match in model | Age at snapshot | State |
|---|---|---|---|
| ENG1 | 2026-10-12 | 1 d | FRESH |
| ENG2 | 2026-10-11 | 2 d | FRESH |
| ENG3 | 2026-10-10 | 3 d | FRESH |
| ENG4 | 2026-10-10 | 3 d | FRESH |
| ESP1 | 2026-10-12 | 1 d | FRESH |

- Model snapshot `current_snapshot_f07238fc…`, `model_as_of` and `source_match_cutoff` 2026-10-12T23:59:59Z. It was built at the 2026-10-12 22:58Z wake; predictions were regenerated 03:33Z on 2026-10-13.
- **The ENG3/ENG4 "Oct 3" state was superseded.** Snapshot `…556787` (as_of 2026-10-03) was last used 2026-10-12 18:42Z.
- Freshness depends on wakes: a new model state only appears when the container starts. This has to be re-verified on `/api/monitoring/football` at RC step 6, before issuance is enabled.

Issuance volume, from the same window:
- 49 READY of 58 eligible fixtures (84%), 52 READY with the #112 aliases.
- Each READY fixture yields up to 2 picks (AH + corners).
- **Expected volume: about 100 forward picks per Saturday-to-Friday week, about 80 per weekend round.** For comparison, Oct 10–11 issued 80.

## 3. Scheduler readiness and the always-awake requirement (P0-C)

### Safety (tests in `test_football_model_pick_issuance.py`)

| Case | Result |
|---|---|
| Cold start (no ledger file) | Schema created, picks issued once; a second cycle writes 0 |
| Restart / redeploy | Idempotent. Persisted cycle counter continues. |
| Repeated cycles | `ALREADY_ISSUED`, never rewritten; a later prediction version cannot change a frozen pick |
| Post-kickoff | Never issued: checked in the manifest and again at write time; the CLI shares the rule |
| **Overlapping issuers, stale snapshot (new)** | Under `BEGIN IMMEDIATE`, `model_pick_id` and canonical holder are re-checked. 0 duplicates, 0 false conflicts. |
| **4 concurrent threads, separate connections (new)** | Every pick written exactly once. The canonical match goes to one provider id only. |
| **Same-process overlap (new)** | The issuance loop and refresh loop share a non-blocking lock. The second caller returns `SKIPPED_CYCLE_IN_PROGRESS`. |
| **CLI apply on a match already held by another provider id (new)** | Skipped and reported in `canonical_duplicates_skipped`. Before this fix the CLI had no canonical check. |

**Why the overlap fixes matter.** Production already holds picks under both provider ids for 14 matches, 28 (match, market) pairs. All were written on 2026-10-06T02:15Z by a retrospective CLI apply after the football-data.org → API-Football switch. They are RETRO, so they are excluded from the forward headline, and they are left untouched.

A residual limit remains: canonical identity hashes team names. 9 more football-data.org matches have an API-Football twin under a different spelling, which the canonical check cannot see. That doesn't matter forward-looking, because API-Football is the only fixture provider now.

### Sleep compatibility (`sleepApplication=true` today)

All schedulers are asyncio loops inside the container (the `combined` role on the single `sports-edge-ai` service), so they run only while the container is up.

- **Measured duty cycle:** 4.4%, with 4–5 irregular GitHub wakes per day and gaps of up to 9 h (#114 §1).
- **On each wake, the code supports this sequence:** startup sync (fixtures, predictions, model state) → issuance loop's first cycle immediately after the sync (`test_issuance_loop_waits_for_startup_sync_then_runs_once_per_interval`) → container stops about 10 min later.
- **Under sleep:** the issuance logic still never issues after kickoff, and never issues retrospectively.
- **What sleep breaks:**
  - Fixtures whose first READY prediction falls in a sleep gap before kickoff are never issued. They appear as `kickoff_passed_not_issued`, which is permanent coverage loss.
  - Result sync and settlement lag by up to a gap (the 18 Oct 10 picks below never polled).
  - The prediction refresh loop (hourly, min 15 min) mostly cannot run.
  - The #113 closing window and CLV need the container up around kickoff.
  - Monitoring heartbeats go stale.

**Exact requirement.** Always-awake is required for:
- prediction refresh;
- the closing window and CLV;
- timely settlement;
- the shadow lane.

Model Pick issuance alone does not strictly need it: it needs one wake after each fixture's first READY prediction and before kickoff.

**Lower-cost alternative supported by the current code.** Keep sleeping on, and replace the unreliable GitHub cron with a dependable waker. For example, one Railway cron service doing a single `GET /health` at about 07:00, 11:00 and 16:00 UK time, which must first be fixed (the existing `-v2` waker never reached the app).
- **What each wake does:** runs one startup sync (about 40 API-Football requests, from `operational_used` 40 at the 03:33Z wake), then one issuance cycle.
- **Cost:** about 3% duty, about $0.30–0.50/month, versus about $6–8/month always-on.
- **Gives up:** refresh, closing-window and CLV, near-real-time settlement, and the shadow lane.
- **Coverage risk:** fixtures announced or first made READY less than one wake gap before kickoff (mostly midweek).

**Recommendation:** always-awake (RC step 4) for M1 as specified. The cron alternative is acceptable only for a "Model Picks only" interim, by owner choice.

## 4. Result-reconciliation backlog (P0-D)

Snapshot inventory: **187 open Model Picks past kickoff, on 122 canonical matches.** Two future picks are excluded. The three reported populations were checked by fixture and selection identity:

| Population | Picks | Matches | Overlaps |
|---|---|---|---|
| A. On football-data.org ids (unpollable) | **67** (+1 API-Football twin pick on the same match) | 47 | none with B or C |
| B. Postponed/cancelled | **6** | 3 | **2 picks are also in C** (Rayo Vallecano v Athletic Club, cancelled, 2026-10-10) |
| C. Oct 10–11 forward pending | **20** | 10 | 2 with B |
| D. Other: status never re-polled after kickoff (Sep 19–Oct 10; 31 still PRE_MATCH, 1 HALFTIME) | 64 | 32 | – |
| E. Other: score present, corners missing (Aug 31–Sep 26) | 31 | 31 | – |

Distinct total: 68 + 6 + 18 + 64 + 31 = **187**, matching the inventory. **A + B + C must not be added**: 2 picks are in both B and C.

### Canonical inventory by missing-result reason

| Reason | Provider ids | Earliest kickoff | Provider accessible? | Safe recovery | API-Football cost (est.) | Needs official evidence? |
|---|---|---|---|---|---|---|
| A. football-data.org ids | football-data.org | 2026-08-21 | Not polled by result sync (it only polls API-Football ids) | (1) 18 matches have an exact canonical API-Football twin (17 FINISHED with score). Settle from the twin's verified result, DB-only. (2) 29 matches have no twin. Look up the API-Football id per (league, date): 16 lookups, then batch `/fixtures?ids=` (≤ 20 per request). | 0 for (1); ≈ 16 + 2 (+ up to 29 statistics calls for corner picks) for (2) | Yes. Map only on verified identity (competition, kickoff, both teams). Never synthesise. All 67 are RETRO, so the forward headline is unaffected. |
| B. Postponed/cancelled | API-Football | 2026-10-03 | Yes | **Leave OPEN.** No VOID policy exists. A rescheduled match (Northampton v Oldham, Walsall v Crawley) may settle on the new date if policy allows; Rayo v Athletic (CANCELLED, with a 0–0 score present) needs an official abandonment/cancellation confirmation. | 3 status requests | **Yes.** Owner must approve a VOID policy (for example, void if not played within N days of the original kickoff) before any VOID is written. |
| C. Oct 10–11 pending (excluding B) | API-Football | 2026-10-10T14:00Z | Yes, never polled: the 2-per-cycle budget was spent and the app was asleep | #111 backlog: `SPORTS_AI_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS=14`, `…_STATUS_BATCH_SIZE=20` | 1 batched status request + up to 9 statistics (corners) | Settles from the provider's final result, the normal path |
| D. Status not re-polled | API-Football | 2026-09-19 | Yes | Same #111 backlog (32 matches; needs lookback ≥ 26 days from Oct 14) | 2 batched + up to 32 statistics | Normal path |
| E. Corners missing | API-Football | 2026-08-31 | Yes | Corner statistics backfill through the backlog's spare slots | up to 31 | Normal path; AH legs are already settled |

**Total one-off cost: about 120 API-Football requests.** That is about 2.7% of the 4,500/day operational cap (daily limit 7,500, 41 used at the snapshot). It stays within the existing plan, with no new provider, but it is still extra paid traffic and needs owner approval (RC step 8).

Not done here: no synthetic fixtures, no forced VOIDs, no historical rewrites. The 28 historical dual-id pairs stay as written.
