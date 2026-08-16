# Experiment Log

Last updated: Saturday, August 15, 2026

## EXP-000 - Historical Football Close Discrepancy Lane

- `date`: 2026-08-14
- `scope`: historical football `1X2` close-vs-close comparison
- `purpose`: test whether retail close prices that differ from Pinnacle close would have produced useful research signals
- `status`: run and graded
- `implementation`: `code/src/sports_ai/experiments/football_1x2_close_value.py`
- `classification`: `RESEARCH_SIGNAL`, not live paper/shadow candidate output
- `important limitation`: this lane uses historical closing prices and does not prove decision-time CLV
- `current interpretation`: useful as a market-discrepancy study, not useful as proof of a live betting edge

## EXP-001 - Opening-Week Football Paper/Shadow Trial

- `date`: 2026-08-14
- `target execution window`: Saturday, August 15, 2026 fixtures
- `scope`: live pre-match football watchlist using blind Poisson fair price, prediction lock, current price snapshot, and gatekeeping
- `target competitions`: La Liga plus EFL divisions with validated history
- `publish rule`: maximum two outputs, never force exactly two
- `allowed decisions`: `PASS`, `WATCH`, `PAPER_SHADOW_CANDIDATE`
- `goals`:
  - verify that the system can create blind pre-market predictions
  - lock the prediction before market comparison
  - record source-backed current prices with timestamps
  - log any qualifying paper/shadow trial candidates before kickoff
  - append close and settlement later
- `success condition`: the process is honest, timestamped, and auditable even if no candidate qualifies
- `current operational state as of Saturday, August 15, 2026`: the live watchlist now runs from the persistent review app with promoted `poisson_1x2_v3`, but today's auto-run still returned `fixtures_discovered = 0` and `final_paper_shadow_candidates = 0`

## EXP-002 - Prior-Season Walk-Forward Poisson Baseline

- `date`: 2026-08-14
- `scope`: blind football `1X2` replay with prior-season context available before each 2025/26 target event
- `implementation`: `code/src/sports_ai/experiments/football_poisson_backtest.py`
- `data state`:
  - `4,832` stored events
  - `17,913` stored market quote rows
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2425` and `2526`
- `target predictions`: `2,284`
- `history mode`: all stored prior competition matches before each target event
- `market benchmark`: devigged Bet365 `1X2`
- `metrics`:
  - model multiclass log loss: about `1.04635`
  - market multiclass log loss: about `1.01404`
  - model multiclass Brier: about `0.62967`
  - market multiclass Brier: about `0.60796`
  - candidates: `754`
  - candidate ROI: about `-14.16%`
- `conclusion`: reject as a betting strategy, retain as the baseline benchmark model
- `interpretation`: prior-season replay improved the historical semantics, but the blind Poisson fair-probability model still remains weaker than the bookmaker market

## EXP-003 - Track B Frozen-Split Model Selection (v1 vs v2)

- `date`: 2026-08-14
- `scope`: football Track B model selection using frozen season splits
- `implementation`:
  - `code/src/sports_ai/experiments/football_track_b_model_selection.py`
  - `code/src/sports_ai/agents/football/poisson_1x2_v2.py`
- `data state`:
  - `12,080` stored events
  - `46,904` stored market quote rows
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2122`, `2223`, `2324`, `2425`, `2526`
- `split`:
  - train `2122-2324`
  - validation `2425`
  - final test `2526`
- `models compared`:
  - `football_poisson_1x2_v1`
  - `football_poisson_1x2_v2` recency-weighted Poisson
- `validation metrics`:
  - `v1` log loss about `1.04225`
  - `v2` log loss about `1.07688`
  - `v1` candidate ROI about `-10.82%`
  - `v2` candidate ROI about `-11.42%`
- `final test metrics`:
  - `v1` log loss about `1.05684`
  - `v2` log loss about `1.07613`
  - `v1` candidate ROI about `-18.60%`
  - `v2` candidate ROI about `-16.56%`
- `selection outcome`: keep `v1` as the active benchmark
- `conclusion`: reject `v2` as the next promoted model
- `interpretation`: the Track B framework is now working correctly because it can test, compare, and reject a model that became more aggressive without becoming more accurate

## EXP-004 - Track B Protocol Tightening and Validation-Only v3 Promotion

- `date`: 2026-08-14
- `scope`: refine Track B research discipline before continuing model-family development
- `implementation`:
  - `code/src/sports_ai/experiments/football_track_b_model_selection.py`
  - `code/src/sports_ai/experiments/football_track_b_finalization.py`
  - `code/src/sports_ai/agents/football/poisson_1x2_v3.py`
- `protocol change`:
  - development mode now evaluates models on validation only
  - finalization mode is a separate command for one selected model on one held-out season
  - probability-model metrics are separated from betting-policy diagnostics
  - calibration buckets are now reported by selection
- `important note`:
  - the `2526` season has already been viewed in earlier cycles
  - it should now be treated as historical evidence, not as a perfectly untouched future test set
- `v3 model family`:
  - blind opponent-adjusted attack and defence strengths
  - regularized team parameters
  - bookmaker information remains excluded from the model
- `validation window`: `2425`
- `train/history window`: `2122-2324`
- `initial v3 result`:
  - `strength_regularization=0.10`
  - log loss about `1.04659`
  - worse than `v1`, not promotable
- `validation-only sweep result`:
  - best observed configuration: `strength_regularization=0.03`
  - `v1` log loss about `1.04225`
  - `v3` log loss about `1.04067`
  - `v1` Brier about `0.62716`
  - `v3` Brier about `0.62615`
- `promotion assessment`:
  - better overall validation log loss than `v1`
  - Brier no worse than `v1`
  - no major degradation by league
  - no major degradation by home/draw/away class
  - therefore eligible for controlled finalization
- `artifact`:
  - `research/football_research/track_b_development_v3_reg003_2026-08-14.json`
- `conclusion`: `v3` is the first Track B development candidate that legitimately clears the current validation-only promotion gate
- `interpretation`: the research framework is now doing two useful things:
  - rejecting weak experimental models like `v2`
  - allowing a stronger model like tuned `v3` to advance without reopening the viewed test season during development

## EXP-005 - Controlled Finalization of v3 vs v1 on 2025/26 Holdout

- `date`: 2026-08-14
- `scope`: controlled Track B finalization for frozen `v1` versus selected `v3`
- `implementation`:
  - `code/src/sports_ai/experiments/football_track_b_finalization.py`
- `important test-status note`:
  - `2526` had already been inspected during earlier `v1/v2` work
  - treat this as a previously observed holdout rather than a pristine untouched test
- `history window`:
  - `2122`, `2223`, `2324`, `2425`
- `held-out season`:
  - `2526`
- `models compared`:
  - `football_poisson_1x2_v1`
  - `football_poisson_1x2_v3`
- `frozen v3 configuration`:
  - `strength_regularization = 0.03`
- `finalization metrics`:
  - `v1` log loss about `1.05684`
  - `v3` log loss about `1.04542`
  - `v1` Brier about `0.63648`
  - `v3` Brier about `0.62901`
  - log loss delta about `-0.01142` in favor of `v3`
  - Brier delta about `-0.00747` in favor of `v3`
- `league diagnostics`:
  - `v3` improved `ENG1`, `ENG2`, `ENG3`, and `ENG4`
  - `ESP1` was effectively flat, with a very small deterioration of about `+0.00041` log loss
- `home/draw/away diagnostics`:
  - binary log loss improved for `home`, `draw`, and `away`
  - absolute calibration gap slightly worsened for `home` and `draw`, but within the current guardrails
  - absolute calibration gap improved for `away`
- `secondary policy note`:
  - candidate ROI remained negative and was worse than `v1`
  - that did not control the model-promotion decision
- `classification`: `PROMOTE`
- `conclusion`: `v3` survived controlled finalization and is the strongest historical football probability model version currently available in the repo
- `artifacts`:
  - `research/football_research/track_b_finalization_v1_2026-08-14.json`
  - `research/football_research/track_b_finalization_v3_reg003_2026-08-14.json`
  - `research/football_research/track_b_finalization_summary_v1_vs_v3_2026-08-14.json`

## EXP-006 - Validation-Only v4 Dixon-Coles Layer on Top of v3

- `date`: 2026-08-14
- `scope`: next Track B experiment after promoted `v3`
- `implementation`:
  - `code/src/sports_ai/agents/football/poisson_1x2_v4.py`
- `model lineage`:
  - `v1` simple Poisson baseline
  - `v2` recency-weighted Poisson rejected
  - `v3` opponent-adjusted Poisson promoted
  - `v4` Dixon-Coles low-score correction layered on top of frozen `v3` strengths
- `development protocol`:
  - validation season only: `2425`
  - history window: `2122-2324`
  - no use of the previously observed `2526` holdout during this step
- `frozen upstream configuration`:
  - `strength_regularization = 0.03`
- `rho sweep`:
  - tried `-0.20`, `-0.15`, `-0.10`, `-0.05`, `0.00`, `0.05`
  - best observed development-time setting: `dixon_coles_rho = -0.05`
  - sweep artifact: `research/football_research/track_b_v4_rho_sweep_2026-08-14.json`
- `validation comparison`:
  - `v3` log loss about `1.04067`
  - `v4` log loss about `1.04011`
  - `v3` Brier about `0.62615`
  - `v4` Brier about `0.62594`
- `promotion assessment versus frozen v3`:
  - better overall validation log loss
  - Brier no worse
  - no major league degradation
  - no major home/draw/away degradation
  - therefore eligible for controlled finalization
- `secondary policy note`:
  - candidate ROI remained negative
  - that does not control the model decision
- `artifact`:
  - `research/football_research/track_b_development_v4_rho_neg005_2026-08-14.json`
- `conclusion`: `v4` is the next promising Track B candidate and merits controlled finalization against frozen `v3`
- `interpretation`: the score-distribution layer appears to add a small but real validation improvement on top of opponent-adjusted expected goals, but the gain is modest and still requires held-out confirmation

## EXP-007 - Chronological Football Shots GAP Walk-Forward

- `date`: 2026-08-15
- `scope`: first deterministic chronological football GAP replay using `shots` only
- `implementation`:
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
  - `code/src/sports_ai/agents/football/gap_ratings.py`
- `input scope`:
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2122`, `2223`, `2324`, `2425`, `2526`
  - only readiness-approved league-seasons
- `initialization policy`:
  - per competition, derive home/away shots baselines from the earliest approved season only
  - process that season as warm-up
  - exclude warm-up rows from evaluation diagnostics
- `season policy`:
  - carry state forward across seasons inside the same competition
  - do not mix competitions
  - teams newly appearing in a competition start from the competition baseline with zero current-competition matches
- `learning rate`: `0.05` labeled `REFERENCE / UNOPTIMIZED`
- `mechanics verified`:
  - prediction recorded before update
  - deterministic replay on fresh engines matches exactly
  - duplicate event ids are rejected
  - market data is not joined into the experiment
- `counts`:
  - matches processed: `12,080`
  - predictions produced: `12,080`
  - evaluation predictions: `9,664`
  - skipped events: `0`
- `overall evaluation diagnostics`:
  - home MAE about `3.74111`
  - away MAE about `3.44959`
  - combined MAE about `3.59535`
  - home mean error about `+0.31581` as observed-minus-expected
  - away mean error about `-0.20575` as observed-minus-expected
- `artifacts`:
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_manifest.json`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_predictions.jsonl`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_final_state.json`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15.md`
- `conclusion`: the first historical shots GAP path is now mechanically reproducible and temporally valid enough to support the next statistic-specific runner
- `interpretation`: these diagnostics validate chronology and state behavior only; they do not constitute model selection, betting validation, or evidence of edge

## Experiment Template

Use this structure for every future experiment:

- `experiment_id`
- `date`
- `hypothesis`
- `data_version`
- `feature_version`
- `model_version`
- `strategy_version`
- `train_window`
- `validation_window`
- `test_window`
- `sample_size`
- `metrics`
- `result`
- `conclusion`

Rules:

- failed experiments remain logged
- historical results are never rewritten after the fact
- research-only lanes must not be presented as validated live strategy outputs
