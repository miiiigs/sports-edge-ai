# Football model accuracy and high-confidence picks audit

Status: research audit, 2026-10-13. No model, flag, ledger or production record was
changed.

Evidence:
- **Production:** a read-only snapshot taken 2026-10-12 15:22Z.
- **Historical:** the Day 10 walk-forward prices (`football_day10_market_pricing_2026-08-18`, 9,664 fixtures, 2022/23–2025/26), graded against `research/football_research/store/football_research.sqlite`.
- **Replay tool:** `python -m sports_ai.experiments.football_model_pick_confidence_replay`.
- **Replay summary:** `research/product_outputs/football/model_pick_confidence/football_model_pick_confidence_replay_2026-10-13.json`.

## 1. Architecture: what actually runs

| Component | State | Evidence |
|---|---|---|
| Route B composite: GAP shots/SOT/corners → NNLS xG → Dixon-Coles → Day 10 pricing | **The only live model** | `dashboard/prediction_service.py:279-589` |
| Route A direct classifier (BTTS, O2.5) | REJECTED (log loss worse than the base rate) | `research/product_outputs/football/route_a/ROUTE_A_VALIDATION.md` |
| Agent 3 | NOT IMPLEMENTED | `dashboard/services.py:6460-6463` |
| Manager consensus (quorum 2, max spread 0.10) | Can never pass with one supported agent; shadow ledger only | `storage/football_edge_desk.py:316-342,1287-1446` |
| Calibration | None at runtime. Platt diagnostics are shadow-only. | `dashboard/football_calibration.py:1-21` |
| Context veto | Kill-only, shadow by default; never changes probabilities | `dashboard/football_context_veto.py` |
| Model Pick policy | Balanced line (minimises \|fair_pos − fair_neg\|), lower-fair side, **no confidence filter** | `dashboard/model_pick_policy.py:93-134` |

There is no multi-agent agreement to audit: every market structurally ends
`INSUFFICIENT_AGENTS`. "Agreement/veto status" on any pick today is therefore
`SINGLE_AGENT`.

**Architectural gaps found** (references are main `8f9b7e2`):

1. **Probability semantics.** `model_probability` stores Day 10
   `effective_win_stake_probability`, which is unconditional: push mass is in the
   denominator. The UI labels it "Model prob". On an AH 0.0 line, a 54% proposition
   is shown as 39.7%. The calibrated, break-even-comparable quantity is
   q = 1 / fair price.
2. **The headline metric is a third definition.** "Weighted Success" =
   (FW + ½HW) / (FW + HW + HL + FL) understates the stake-weighted win share
   W / (W + L). It is 48.7% vs 52.3% on the replay's balanced AH picks.
3. **Live parameters are fitted in-sample** on the full history (conversion, rho,
   corner dispersion), and the out-of-sample rho is discarded
   (`snapshot_service.py:664-722`). The replay below uses the walk-forward fits,
   so live behaviour may differ slightly.
4. The team-history gate admits a team after **one** observed match
   (`snapshot_service.py:209`). Team ids are scoped by competition, so promoted
   teams start from zero (`:441`).
5. `safeguard_status` is hard-coded BLOCKED on every READY prediction
   (`prediction_service.py:583-585`), so it carries no information.
6. No historical AH or totals prices are ingested (`football_history.py:216-221`),
   so EV and CLV for AH and corners cannot be backtested.
7. The bundled research DB ends 2026-05-24. Freshness depends entirely on the
   6-hourly research refresh succeeding in production.

## 2. True accuracy baseline, cohorts never combined

Win share = stake-weighted W / (W + L), with pushes excluded; 95% Wilson
intervals. The product's Weighted Success is given in brackets where it differs.

| Cohort | Settled | FW/HW/P/HL/FL | Win share | 95% CI | ROI |
|---|---|---|---|---|---|
| **Genuine pre-kickoff Model Picks** (production, all markets) | 47 | 18/4/3/2/20 | 48.8% (WS 45.5%) | 34.3–63.5% | n/a (no odds) |
| – AH | 24 | 9/2/2/2/9 | 50.0% | 29.9–70.1% | n/a |
| – Corners | 23 | 9/2/1/0/11 | 47.6% | 28.3–67.6% | n/a |
| Retrospective backfilled Model Picks (**not forward evidence**) | 321 | 146/17/14/12/132 | 52.8% (WS 50.3%) | 47.1–58.5% | n/a |
| Paper selections, counted (non-AH) | 816 | 294/12/28/18/464 | 38.8% | 35.4–42.3% | **−12.8%** (−104.37u, avg odds 2.62) |
| Paper legacy AH (quarantined, invalid evidence) | 778 | – | not reported | – | – |
| Strong Value Bets | **0** | – | – | – | – |
| Historical walk-forward, balanced AH (replay) | 9,664 | 4067/681/608/571/3737 | 52.3% | 51.2–53.3% | n/a |
| Historical walk-forward, balanced corners (replay) | 9,664 | 4546/148/346/157/4467 | 50.4% | 49.4–51.4% | n/a |

**Paper, by market:**

| Market | Positions | Win share | ROI |
|---|---|---|---|
| Corners | 283 | 53.8% | +9.9% |
| Goals | 358 | 31.0% | −24.7% |
| 1X2 | 131 | 23.1% | −26.9% |
| BTTS | 48 | 37.5% | −18.9% |

**Paper, by league (ROI):** ENG4 +11.0%, ENG1 −0.2%, ESP1 −17.9%, ENG2 −22.9%, ENG3 −34.6%.

**Paper, by entry odds:**

| Entry odds | Win share | ROI |
|---|---|---|
| < 1.70 | 64.0% | +1.6% |
| ≥ 3.00 | 18.5% | −27.0% |

All production Model Picks and paper positions use model version
`football_dashboard_current_snapshot_v1`, so there is no cross-version comparison.

**Paper calibration.** On WON/LOST rows, ECE is 0.088. The model is overconfident
below p = 0.55 (mean p 0.45 → 0.34 hit rate). That is the winner's curse expected
when positions are selected for disagreeing with the market.

## 3. Can selective picks reach 65–70%?

All rules below were chosen on 2022/23–2023/24 and evaluated, frozen, on
2024/25–2025/26.

| Rule | Holdout win share | Holdout coverage | Mean fair price | Notes |
|---|---|---|---|---|
| Balanced AH, no filter (production) | 52.5% | 100% | 1.92 | |
| Balanced AH, q ≥ 0.52 | 54.5% | 47% | 1.88 | |
| Balanced AH, q ≥ 0.54 | 65.9% | **5%** | 1.74 | Only fixtures where the −1…+1 catalog cannot balance (strong favourites) |
| Balanced corners, q ≥ 0.54 / 0.60 | 51.2% / 52.2% | 8% / 2% | 1.73 / 1.59 | **No discrimination** |
| Threshold reaching 65% or 70% with ≥ 200 selection-season picks | **none exists** for AH or corners | | | |
| Highest-q AH line with q ≥ 0.70 | **75.0%** | 100% | 1.38 | Break-even 1.33 |
| Highest-q corner line with q ≥ 0.70 | 65.9% | 34% | 1.40 | Below its own q of 71.6% |
| 1X2 selection with q ≥ 0.65 | 80.3% | 3% | 1.44 | ROI at Pinnacle close +2.7% (n=117, CI includes 0) |

**Conclusions:**

- **Not feasible for the production balanced-line policy.** Balanced-line Model
  Picks cannot reach 65–70% under a leakage-safe protocol, because the policy is a
  coin flip by construction. AH shows a small real lean (52–53%), and corners
  show none.
- **Trivially reachable by line choice.** Picking the line where q ≥ 0.70 gives
  about 75% on AH, holdout, for every fixture. That number measures line choice,
  not skill. At fair odds of 1.38 the win rate needed to break even is 72–73%,
  so EV depends entirely on whether a bookmaker offers more than the model's fair
  price.
- **The model shows no information beyond the market where it can be tested.**
  On 1X2 (holdout n = 3,417):
  - log loss is model 1.0342 vs Pinnacle close 1.0098;
  - the optimal model weight in a log-pool blend, fitted on the selection
    seasons, is **0.0**, and the market is better in every league;
  - when the model is 3–12 points more bullish than the market, those selections
    win at the market-implied rate and lose 3.6–5.7% at the close.

  So a high win rate and positive EV are separate questions, and on current
  evidence the model does not supply the second.
- **A 65–70% target is not a quality objective.** It can be met while losing
  money, as the paper book shows: < 1.70 odds win 64.0% for +1.6% ROI, and corners
  at p ≥ 0.70 win 63.6% for −0.4%. Use calibration plus EV/CLV against the market
  instead.

## 4. Calibration, out of sample (holdout seasons, q-based)

| Market | ECE | Log loss | Notes |
|---|---|---|---|
| Asian Handicap | 0.011 | 0.5821 | Well calibrated across 0.05–0.95 |
| Goals totals | 0.004 | 0.6438 | |
| BTTS | 0.004 | 0.6896 | |
| 1X2 | 0.008 | 0.6046 (binary) | |
| **Corners** | **0.026** | 0.6805 | **Overconfident tails**: predicted 0.73 → 0.67 observed, 0.82 → 0.71, 0.27 → 0.33 |

**Corner temperature scaling** (T fitted on the selection seasons only): T = **1.20**.
Holdout ECE falls from 0.026 to **0.011** and log loss from 0.6805 to 0.6786.

## 5. Two separate outputs

**A. High-confidence Model Picks (shadow track; proposed, not implemented).**
- Source: the first pre-kickoff READY prediction, frozen as today (forward-only rules unchanged).
- Confidence = q = 1/fair on the selected line, shown as "win probability excl. push".
- Selection: AH only, the highest-q catalog line with q ≥ 0.70. Corners are excluded until corner calibration ships and is re-validated.
- Agreement/veto status: `SINGLE_AGENT`, plus the context-veto shadow result and all prediction blockers.
- Reported separately from balanced-line Model Picks, with forward win share and Wilson CI, calibration (mean q vs realised), mean fair price and coverage.
- Expected forward win share ≈ 72–77% (replay holdout). **This is not a value claim.**

**B. Strong Value Bets (existing value lane; unchanged, still disabled).**
- Requires every Model Pick safeguard.
- Bookmaker odds must be valid and fresh, from an accepted ingestion version.
- Edge = W·(price−1) − L must exceed the competition threshold (0.05–0.08), with de-vigged reference books.
- Staking: quarter Kelly, capped at 2%.
- ROI and CLV are tracked independently of Model Picks.
- The evidence above says this lane should not be published until forward CLV is positive (section 6).

## 6. Forward-only shadow-validation protocol

1. **Freeze** before the forward window opens: model version, policy (A and B definitions above), thresholds, corner T. No change during a window.
2. **Record** only pre-kickoff rows (`created_at < kickoff_utc`), forward-only issuance as in #110. Retrospective rows are never pooled.
3. **Measure per cohort and market:**
   - win share with Wilson CI;
   - mean q vs realised (calibration);
   - coverage;
   - mean fair price;
   - for B: entry price vs fair, ROI, and **CLV against the ≤ 60-minute closing snapshot** (#113).
4. **Sample size.** A stated win share needs ≈ 300 settled picks per cohort for a ±5-point 95% interval. ROI needs far more: about 1,000+ at odds near 2.0 for ±6 points. Report "insufficient sample" below these.
5. **Promotion gate for B** (publication): forward CLV > 0 with a 95% CI excluding 0 over ≥ 500 bets, and calibration ECE ≤ 0.03. Win rate is not a promotion criterion.
6. **Kill criteria:**
   - forward ECE > 0.05 over 300 picks;
   - CLV ≤ 0 over 500 bets;
   - any retrospective row reaching a forward metric.

## 7. Proposed changes and expected impact

| # | Change | Evidence | Expected impact | Status |
|---|---|---|---|---|
| 1 | Leakage-safe confidence replay harness | This audit | Reproducible threshold/calibration validation; selection and holdout separation enforced by tests | **This PR** |
| 2 | Show q = 1/fair as "win probability (excl. push)", and add stake-weighted win share next to Weighted Success | §1 gaps 1–2, §2 | Model Pick confidence becomes interpretable and calibratable. No accuracy change, but reported numbers rise honestly (balanced AH 48.7% → 52.3%), so both are shown. | **#118** (stacked on #110) |
| 3 | Corner temperature T = 1.20 (flag, default off) | §4 | Corner ECE 0.026 → 0.011, log loss −0.0019. Balanced-line selections unchanged; corner value edges more conservative. | **#117** (flag default off; enabling needs owner approval) |
| 4 | High-confidence AH shadow track (output A) | §3 | ≈ 75% forward win share expected at fair ≈ 1.38; no EV claim | Proposed (shadow only) |
| 5 | Shrink toward the market / market-informed prior before any value publication | §3 (blend weight 0.0, negative ROI on disagreement) | Prevents publishing systematic −4 to −6% disagreement bets | Required before B |
| 6 | Ingest historical AH, OU and corner closing odds (football-data.co.uk AH/OU columns) | §1 gap 6 | Makes AH/corner EV and CLV testable historically | Proposed |
| 7 | Point-in-time live fits (use out-of-sample rho and prior-season conversion) | §1 gap 3 | Removes in-sample optimism in live parameters | Proposed |
| 8 | Team-history minimum (e.g. ≥ 5 matches) and promoted-team carry-over | §1 gap 4 | Fewer early-season blind predictions; needs its own replay before adoption | Proposed |
| 9 | Drop the 1X2 and goals paper lanes from any headline, or gate them behind market-blend evidence | §2 paper by market | Stops −25% lanes diluting evidence | Proposed |

## 8. Remaining blockers

- **Sample size:** only 47 forward settled Model Picks. No forward accuracy claim is possible yet.
- **No historical AH or corner odds:** EV for those markets cannot be validated before forward collection (#113).
- **The 1X2 benchmark is the only market-relative test,** and the model loses to the market there.
- **The replay uses walk-forward fits, not the live in-sample fits** (gap 3).
- **Production depends on M1** (always-on, forward issuance, fresh research) for any forward data to accumulate.

## 9. High-confidence AH lane vs market (Phase C, 2026-10-13)

Command:

```
python -m sports_ai.experiments.football_high_confidence_benchmark --day10-prices <jsonl> --research-db <sqlite> --output <json>
```

Evidence: `research/product_outputs/football/model_pick_confidence/football_high_confidence_benchmark_2026-10-13.json`.

**Market baseline.** Independent Poisson means are solved per fixture to the de-vigged Pinnacle closing 1X2. Every AH catalog line is then priced through the paper settlement engine. This is an approximation of a real AH market, because no historical AH prices exist. It is itself well calibrated: holdout ECE 0.0077.

**Holdout 2024/25–2025/26** (4,832 fixtures; Pinnacle available for 3,417). One pick per fixture, threshold 0.70 pre-registered:

| Lane | Picks | Coverage | Win share | 95% CI | Mean q (own) | Mean market q | Mean fair |
|---|---|---|---|---|---|---|---|
| **MODEL_HC** (q ≥ 0.70, ≥ 2 venue matches) | 4,738 | 98% | **74.8%** | 73.5–76.1 | 72.7% | 73.7% | 1.38 |
| MARKET_HC (market q ≥ 0.70) | 3,417 | 71% | 73.3% | 71.7–74.9 | 73.2% | 73.2% | 1.37 |
| FAVOURITE_DNB (market favourite, AH 0.0) | 3,417 | 71% | 67.6% | 65.8–69.4 | – | 66.4% | 1.55 |
| FAVOURITE_WIN (market favourite, AH −0.5) | 3,417 | 71% | 50.1% | 48.4–51.7 | – | 49.3% | 2.12 |
| BALANCED (production Model Pick) | 4,832 | 100% | 52.5% | 51.0–54.0 | 52.1% | 52.4% | 1.92 |

The 2022/23–2023/24 selection seasons agree: MODEL_HC 71.8%, MARKET_HC 72.6%, FAVOURITE_DNB 67.5%.

**Paired test on identical bets.** On MODEL_HC's own (fixture, line, side), n = 3,323 with market prices:
- model log loss 0.5625 vs market 0.5408;
- difference **+0.0217, 95% CI [+0.0146, +0.0288]**, cluster bootstrap by match date.

Selection seasons: +0.0318 [+0.0263, +0.0375]. **The market prices the model's high-confidence bets significantly better than the model does.** It also rates them more likely (73.7% vs 72.7%), so real offered prices will be shorter than model fair.

**By league (holdout MODEL_HC):** ENG1 75.4%, ENG2 74.3%, ENG3 73.7%, ENG4 74.9%, ESP1 76.4%. No league departs from the pooled result.

**Exploratory thresholds** (4 extra; not used for any decision): MODEL_HC holdout
- 0.60 → 63.8%
- 0.65 → 68.7%
- 0.75 → 79.5%
- 0.80 → 84.5%

At every threshold the win share is about the same as the market lane's. It tracks the threshold, not skill.

**Earlier frozen model versions.** No earlier version has AH outputs. Day 10 v1 is the only priced replay. Route A was rejected, and legacy Poisson v1–v4 is 1X2-only. This comparison is therefore not possible without re-running Days 8–10 on an alternative configuration.

**Conclusion.** A 70% high-confidence AH lane is reproducible (≈ 72–75%), but a model-free market lane matches it, and the market is the better-calibrated probability on the same bets. Treat the lane as accuracy tracking only. A value claim needs real accepted prices showing offered odds above model fair, and the evidence predicts the opposite.
