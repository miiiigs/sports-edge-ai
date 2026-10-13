# High-confidence AH shadow lane

Status: implemented and **disabled**, 2026-10-13. Policy `football_high_confidence_ah_shadow_v1`.

## What it does

For each upcoming fixture, once per cycle:

1. **Frozen source.** Take the first pre-kickoff READY prediction. The fixture is
   valid only while its latest prediction is still READY; otherwise abstain with
   `LATEST_PREDICTION_NOT_READY`.
2. **Vetoes** (each one is a recorded abstention, so zero picks is a valid outcome):

   | Veto | Rule |
   |---|---|
   | `MODEL_STATE_NOT_BEFORE_KICKOFF` | Model-state date must be strictly before the kickoff date |
   | `PREDICTION_BLOCKERS_PRESENT` | The prediction has blockers |
   | `TEAM_IDENTITY_UNRESOLVED` | Either team's identity is unresolved |
   | `INSUFFICIENT_VENUE_HISTORY` | Fewer than 2 prior venue matches for either team (evidence: #119 / #120) |
   | `CANONICAL_DUPLICATE` | A second provider id for the same match; API-Football is preferred; never merged |
   | `NO_LINE_AT_THRESHOLD` | No AH line reaches the threshold |

3. **Line.** The AH catalog line with the longest fair price among those with
   q = 1/fair ≥ **0.70**. q is used uncalibrated because holdout AH ECE was 0.011.
4. **Record.** Line, side, q, the unconditional stake probability, model fair
   price, prediction id, snapshot id, model versions, `model_as_of` and venue
   history counts.
5. **Offered price**, stored separately and only when available: the best fresh,
   pre-kickoff, value-eligible snapshot for the **exact** line and side. That
   means accepted ingestion versions only (legacy v1 API-Football AH is
   excluded) and the provider's actionable max age. The model fair price is
   never replaced.
6. **Settlement** uses the Model Pick verified-truth engine
   (`build_settlement_plan`): FINISHED with a final score only, exactly once.

**Isolation.**
- Separate SQLite file (`football_high_confidence_shadow.sqlite`, next to the Model Pick DB; overridable with `SPORTS_AI_HIGH_CONFIDENCE_SHADOW_DB_PATH`).
- The dashboard and paper DBs are read-only.
- The lane never imports the Model Pick policy or ledger, and never creates paper positions or Value Bets. This is pinned by tests.
- Entries cannot be updated or deleted except to fill settlement once (database triggers).

**CLI.** Dry-run by default; `--apply` writes the shadow DB:

```
python -m sports_ai.dashboard.high_confidence_shadow --dashboard-db <dash> --paper-db <paper> --shadow-db <shadow> [--apply]
```

**Production dry run** (snapshot 2026-10-12 15:22Z, no write):
- 58 upcoming fixtures evaluated, 7 issued (the 7 with a READY latest prediction);
- 34 abstained `LATEST_PREDICTION_NOT_READY`, 17 `NO_PRE_KICKOFF_READY_PREDICTION`;
- mean fair price 1.385;
- 0 offered prices, because no fresh v2 AH snapshots exist for those fixtures yet.

## What to expect

| Quantity | Historical replay (holdout) | Production today |
|---|---|---|
| Coverage of evaluable fixtures | 98% | 12% (7/58). Bounded by READY predictions; rises with M1 always-on and fresh research. |
| Win share | 74.8% [73.5, 76.1] | – |
| Mean q | 72.7% | – |
| Mean fair price | 1.38 | 1.385 |
| Model-free market lane, same rule | 73.3% | – |
| Market favourite draw-no-bet | 67.6% | – |

**Expected volume.** About 56 fixtures per matchweek across ENG1–4 and ESP1, times
the READY rate, times about 98%. At a 60–90% READY rate that is roughly 30–50 picks
per matchweek. 300 settled picks (a ±5-point CI) takes about 6–10 matchweeks.

**Value.** None is claimed. On identical bets the market-implied probability beats
the model's: log loss +0.022 [0.015, 0.029] in the market's favour (#116 §9). The
market rates these sides more likely than the model, so expect offered prices
*below* model fair. `mean_offered_over_fair` is tracked to test exactly that.

## Forward validation protocol

1. **Freeze** the policy, threshold (0.70), venue-history minimum (2), model
   version and calibration id before enabling. Any change starts a new
   `policy_id`; cohorts are never pooled across ids.
2. **Forward only:** `issued_at < kickoff_utc` on every row, enforced at write.
   No backfill path exists.
3. **Report** from `performance()` only:
   - stake-weighted win share with Wilson CI;
   - n, coverage (issued / evaluated), abstention reasons;
   - mean q vs realised (`calibration_gap`);
   - mean model fair price;
   - offered-price count and mean offered / fair.
4. **Comparators** over the same window:
   - balanced-line Model Picks (forward cohort);
   - the market lane and favourite DNB, recomputed from recorded closing prices when #113 closing capture is live.
5. **Minimum sample** before any statement: 300 settled picks. Below that,
   `insufficient_sample` is true and only counts are shown.
6. **Pass / fail** (no value implication):
   - calibration gap within ±0.04;
   - win share CI overlapping the replay's 72–77%.
7. **Kill:**
   - calibration gap beyond ±0.06 after 300 picks;
   - any row with `issued_at >= kickoff_utc`;
   - any write outside the shadow DB.

## Activation gates (each needs explicit approval)

1. M1 released, with app sleeping off and the prediction refresh running, so
   predictions are fresh and READY.
2. #119 merged (provenance), and preferably #120 with
   `SPORTS_AI_FOOTBALL_MIN_VENUE_HISTORY_MATCHES=2`, so the production gate
   matches the lane.
3. The model-freshness gate from the M1 plan holds: the issuing competitions'
   model state contains their latest completed matchday. The lane freezes the
   first READY prediction, exactly as Model Picks do.
4. A scheduler hook, which is a small follow-up after #110 lands, to avoid
   conflicting with #110's worker changes. Until then the lane runs only via
   the CLI.
5. Set `SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW=1`. **Never** connected to Value
   Bet issuance or publication.
