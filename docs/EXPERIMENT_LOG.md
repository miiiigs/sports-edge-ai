# Experiment Log

Last updated: Wednesday, August 19, 2026

## EXP-027 - Football Dashboard V1.2 football-data.org Watchlist Refresh

- `date`: 2026-08-19
- `scope`: switch the dashboard default fixture source to football-data.org, add a dedicated
  system-status page, and harden the watchlist UI around the accepted frozen football outputs
- `implementation`:
  - `code/src/sports_ai/dashboard/app.py`
  - `code/src/sports_ai/dashboard/services.py`
  - `code/src/sports_ai/dashboard/fixture_provider.py`
  - `code/src/sports_ai/dashboard/templates/`
  - `code/src/sports_ai/dashboard/static/dashboard.css`
  - `code/tests/test_football_dashboard.py`
- `verified baseline before coding`:
  - full suite: `202 passed in 56.00s`
- `verified post-implementation baseline`:
  - full suite: `203 passed in 35.13s`
- `runtime verification`:
  - dashboard app booted successfully under Uvicorn
  - verified HTTP `200`:
    - `/status`
    - `/api/system/status`
- `real provider result`:
  - default provider is now `football-data.org`
  - local verification environment on Wednesday, August 19, 2026 did not have
    `SPORTS_AI_FOOTBALL_DATA_TOKEN` set
  - dashboard therefore reported:
    - `fixture_feed.state = NOT_CONFIGURED`
    - `prediction_engine.state = READY`
- `important hardening closed`:
  - football-data.org provider adapter added around `/v4/matches`
  - lightweight provider caching added
  - status page added at `/status`
  - matches/predictions/candidates/detail pages upgraded into a denser watchlist surface
  - next-match queue broadened to the nearest future fixtures
- `conclusion`: Dashboard V1.2 closes the requested football-data.org/UI refresh build honestly,
  but the first real current football-data.org fixture still depends on configuring the token

## EXP-026 - Football Dashboard V1.1 Current Runtime Validation

- `date`: 2026-08-19
- `scope`: harden the operational football dashboard around real current-fixture provider truth,
  fixture-sync diagnostics, refresh control, and pre-match prediction locking
- `implementation`:
  - `code/src/sports_ai/dashboard/app.py`
  - `code/src/sports_ai/dashboard/services.py`
  - `code/src/sports_ai/dashboard/fixture_provider.py`
  - `code/src/sports_ai/dashboard/prediction_service.py`
  - `code/src/sports_ai/dashboard/templates/`
  - `code/tests/test_football_dashboard.py`
- `verified baseline before coding`:
  - full suite: `200 passed in 37.43s`
- `verified post-implementation baseline`:
  - full suite: `202 passed in 29.50s`
- `runtime verification`:
  - local server booted on `http://127.0.0.1:8008`
  - verified HTTP `200`:
    - `/`
    - `/matches`
    - `/predictions`
    - `/candidates`
    - `/api/system/status`
    - `/api/fixtures/today`
    - `/api/fixtures/upcoming`
    - `/api/fixtures/next`
- `real provider result`:
  - API-Football authentication succeeded
  - current season for the August 2026 fixture window is correctly `2026`
  - the configured plan blocked scoped current-season fixture rows with:
    - `Free plans do not have access to this season, try from 2022 to 2024.`
  - the dashboard now reports that state as `fixture_feed.state = DEGRADED`
  - in-scope current dashboard counts remained:
    - `live = 0`
    - `today = 0`
    - `upcoming = 0`
    - `next = 0`
- `prediction/runtime result`:
  - `current_prediction_pipeline_ready = YES`
  - `current_market_decision_ready = NO`
  - `live_paper_pipeline_ready = NO`
  - first real current supported fixture validated through the live provider path: `NO`
- `important hardening closed`:
  - provider query diagnostics persisted and exposed
  - degraded provider state separated from a genuine empty slate
  - `GET /api/fixtures/next` added
  - `GET /api/predictions` added
  - `POST /api/fixtures/refresh` added
  - manual refresh cooldown added
  - first-time post-kickoff prediction generation blocked
  - prediction lock metadata surfaced
- `conclusion`: Dashboard V1.1 closes the bounded runtime-validation hardening pass honestly, but
  the live provider plan restriction still blocks validation of a real current supported fixture
  flowing all the way into a persisted current prediction

## EXP-025 - Operational Football Dashboard V1

- `date`: 2026-08-19
- `scope`: first operational football dashboard for current fixtures and frozen pre-match predictions
- `implementation`:
  - `code/src/sports_ai/dashboard/app.py`
  - `code/src/sports_ai/dashboard/services.py`
  - `code/src/sports_ai/dashboard/fixture_provider.py`
  - `code/src/sports_ai/dashboard/snapshot_service.py`
  - `code/src/sports_ai/dashboard/prediction_service.py`
  - `code/src/sports_ai/dashboard/storage.py`
- `runtime verification`:
  - local FastAPI server booted successfully on `http://127.0.0.1:8008`
  - real `GET /` returned `200`
  - real `GET /api/system/status` reported:
    - `fixture_feed.state = CONNECTED`
    - `prediction_engine.state = READY`
    - `current_model_snapshot.state = READY`
    - `dashboard_ui_ready = YES`
    - `current_fixture_feed_ready = YES`
    - `current_prediction_pipeline_ready = YES`
    - `current_market_decision_ready = NO`
    - `live_paper_pipeline_ready = NO`
- `real provider result`:
  - configured API-Football feed was connected but returned `0` in-scope live fixtures, `0` today fixtures, and `0` upcoming fixtures during the Wednesday, August 19, 2026 verification window
- `classification`: operational product surface, not a historical backtest artifact
- `important limitations`:
  - no live odds-market provider connected yet
  - no point-in-time-valid xG feed connected yet
  - no supported real current fixture was available at verification time to display a generated live example

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

## EXP-008 - Chronological Football Shots-on-Target GAP Walk-Forward

- `date`: 2026-08-17
- `scope`: second deterministic chronological football GAP replay using `shots_on_target` only
- `implementation`:
  - `code/src/sports_ai/experiments/football_gap_shots_on_target_walkforward.py`
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
  - `code/src/sports_ai/agents/football/gap_ratings.py`
- `input scope`:
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2122`, `2223`, `2324`, `2425`, `2526`
  - only readiness-approved league-seasons
- `initialization policy`:
  - per competition, derive home/away SOT baselines from the earliest approved season only
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
  - changing shots columns alone does not alter the SOT replay
  - a readiness-approved row with missing SOT input fails loudly
  - market data is not joined into the experiment
- `counts`:
  - matches processed: `12,080`
  - predictions produced: `12,080`
  - evaluation predictions: `9,664`
  - skipped events: `0`
- `overall evaluation diagnostics`:
  - home MAE about `1.81890`
  - away MAE about `1.67014`
  - combined MAE about `1.74452`
  - home mean error about `+0.09369` as observed-minus-expected
  - away mean error about `-0.08365` as observed-minus-expected
- `artifacts`:
  - `research/football_research/football_gap_shots_on_target_walkforward_reference_2026-08-17_manifest.json`
  - `research/football_research/football_gap_shots_on_target_walkforward_reference_2026-08-17_predictions.jsonl`
  - `research/football_research/football_gap_shots_on_target_walkforward_reference_2026-08-17_final_state.json`
  - `research/football_research/football_gap_shots_on_target_walkforward_reference_2026-08-17.md`
- `conclusion`: the second Day 7 statistic lane is now mechanically reproducible and reuses the same validated chronology shell as `shots` without widening scope into Day 8 work
- `interpretation`: these diagnostics validate chronology and state behavior only; they do not constitute model selection, betting validation, or evidence of edge

## EXP-009 - Chronological Football Corners GAP Walk-Forward

- `date`: 2026-08-17
- `scope`: third deterministic chronological football GAP replay using `corners` only
- `implementation`:
  - `code/src/sports_ai/experiments/football_gap_corners_walkforward.py`
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
  - `code/src/sports_ai/agents/football/gap_ratings.py`
- `input scope`:
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2122`, `2223`, `2324`, `2425`, `2526`
  - only readiness-approved league-seasons
- `initialization policy`:
  - per competition, derive home/away corners baselines from the earliest approved season only
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
  - changing shots columns alone does not alter the corners replay
  - a readiness-approved row with missing corner input fails loudly
  - market data is not joined into the experiment
- `counts`:
  - matches processed: `12,080`
  - predictions produced: `12,080`
  - evaluation predictions: `9,664`
  - skipped events: `0`
- `overall evaluation diagnostics`:
  - home MAE about `2.26330`
  - away MAE about `2.01939`
  - combined MAE about `2.14135`
  - home mean error about `+0.11378` as observed-minus-expected
  - away mean error about `-0.09605` as observed-minus-expected
- `artifacts`:
  - `research/football_research/football_gap_corners_walkforward_reference_2026-08-17_manifest.json`
  - `research/football_research/football_gap_corners_walkforward_reference_2026-08-17_predictions.jsonl`
  - `research/football_research/football_gap_corners_walkforward_reference_2026-08-17_final_state.json`
  - `research/football_research/football_gap_corners_walkforward_reference_2026-08-17.md`
- `conclusion`: the third Day 7 statistic lane is now mechanically reproducible under the same chronology shell as `shots` and `shots_on_target`, so the next bounded task is `lambda` fitting rather than another replay implementation
- `interpretation`: these diagnostics validate chronology and state behavior only; they do not constitute model selection, betting validation, or evidence of edge

## EXP-010 - Day 7 Football GAP Lambda Fitting And Fitted Replay Closure

- `date`: 2026-08-17
- `scope`: walk-forward `lambda` fitting for `shots`, `shots_on_target`, and `corners`, plus fitted replay artifacts with explicit persisted `as_of`
- `implementation`:
  - `code/src/sports_ai/experiments/football_gap_lambda_fitting.py`
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
  - `code/src/sports_ai/agents/football/gap_ratings.py`
- `input scope`:
  - competitions `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
  - seasons `2122`, `2223`, `2324`, `2425`, `2526`
  - only readiness-approved league-seasons
- `fixed lambda grid`:
  - `0.02`
  - `0.05`
  - `0.10`
- `selection discipline`:
  - preserve the existing warm-up season per competition
  - fit separately by competition and statistic
  - use chronological walk-forward validation folds on `2223`, `2324`, and `2425`
  - reserve `2526` as the final holdout during selection
  - retain the `0.05` reference unless a non-reference candidate improves aggregate validation combined MAE by at least `0.01`
- `variant count`:
  - `45` total tested competition/statistic/lambda configurations
- `selected lambdas`:
  - `shots`: `ENG1=0.10`, `ENG2=0.10`, `ENG3=0.10`, `ENG4=0.10`, `ESP1=0.10`
  - `shots_on_target`: `ENG1=0.05`, `ENG2=0.05`, `ENG3=0.05`, `ENG4=0.05`, `ESP1=0.05`
  - `corners`: `ENG1=0.05`, `ENG2=0.05`, `ENG3=0.05`, `ENG4=0.05`, `ESP1=0.05`
- `persisted-output contract`:
  - all three fitted prediction lanes now carry explicit `as_of`
  - all three fitted prediction lanes carry `model_version`
  - GAP state export now carries explicit `as_of`
- `fitted replay diagnostics`:
  - `shots` combined MAE about `3.56459`
  - `shots_on_target` combined MAE about `1.74452`
  - `corners` combined MAE about `2.14135`
- `determinism verified`:
  - repeated fitting runs produce identical variant records
  - repeated fitting runs produce identical selections
  - repeated fitted replay runs produce identical predictions and final state for `shots`, `shots_on_target`, and `corners`
- `artifacts`:
  - `research/football_research/football_gap_lambda_fitting_2026-08-17.json`
  - `research/football_research/football_gap_lambda_fitting_2026-08-17.md`
  - `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17_manifest.json`
  - `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17_predictions.jsonl`
  - `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17_final_state.json`
  - `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17.md`
  - `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17_manifest.json`
  - `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17_predictions.jsonl`
  - `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17_final_state.json`
  - `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17.md`
  - `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17_manifest.json`
  - `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17_predictions.jsonl`
  - `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17_final_state.json`
  - `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17.md`
- `conclusion`: Day 7 is now closed for the currently loaded football scope because the three independent GAP statistic lanes, walk-forward lambda fitting, explicit persisted `as_of`, explicit persisted `model_version`, deterministic replay, and goals prohibition are all backed by tests and artifacts
- `interpretation`: this closes the bounded GAP-statistics foundation only. It does not yet constitute the downstream Day 8 conversion/distribution layer or any market-pricing evidence

## EXP-011 - Day 8 Football Goal Conversion And Corner Distribution Evidence

- `date`: 2026-08-17
- `scope`: convert the frozen Day 7 fitted GAP outputs into downstream expected goals and compare Poisson versus negative-binomial corner distributions without changing GAP state or `lambda`
- `implementation`:
  - `code/src/sports_ai/experiments/football_day8_goal_conversion.py`
  - `code/src/sports_ai/experiments/football_gap_lambda_fitting.py`
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
- `frozen upstream baseline`:
  - `shots`, `shots_on_target`, and `corners` predictions come only from the fitted Day 7 artifacts
  - no goals data is allowed into the GAP input contract
  - no odds, de-vig, edge, stake, or manager logic is joined
- `conversion model`:
  - model version: `football_day8_goal_conversion_v1`
  - form: `league_and_venue_nonnegative_linear_shots_plus_sot_v1`
  - equation: `expected_goals_side = beta_shots * predicted_shots_side + beta_shots_on_target * predicted_shots_on_target_side`
  - fit separately by competition and venue
  - no intercept
- `walk-forward design`:
  - preserve `2122` as the warm-up season inherited from Day 7
  - for each competition, predict `2223`, `2324`, `2425`, and `2526` one season at a time
  - for each predicted season, fit conversion coefficients only on prior seasons from the same competition
  - record fit-period bounds, `as_of`, sample size, and model versions for every fitted conversion record
- `variant count`:
  - `40` competition/season/venue conversion fits recorded
- `prediction count`:
  - `9,664` evaluation fixtures
- `overall expected-goal diagnostics`:
  - mean predicted home goals about `1.50628`
  - mean observed home goals about `1.46058`
  - mean predicted away goals about `1.14088`
  - mean observed away goals about `1.18233`
  - mean predicted total goals about `2.64716`
  - mean observed total goals about `2.64290`
  - total-goal MAE about `1.27561`
  - total-goal bias about `+0.00426`
- `corner distribution comparison`:
  - evaluate Poisson and negative binomial deterministically on each competition/season fold
  - fit negative-binomial dispersion from prior training rows only
  - choose the family used for a season from cumulative prior out-of-sample log likelihood only
  - first evaluation season defaults to Poisson because no prior out-of-sample evidence exists yet
- `selection summary`:
  - final cumulative evidence selected `negative_binomial` for `ENG1`, `ENG2`, `ENG3`, `ENG4`, and `ESP1`
- `determinism verified`:
  - repeated fresh runs returned identical prediction rows
  - repeated fresh runs returned identical conversion fit records
  - repeated fresh runs returned identical corner fold records
  - repeated fresh runs returned identical diagnostics and corner summaries
- `artifacts`:
  - `research/football_research/football_day8_goal_conversion_2026-08-17_manifest.json`
  - `research/football_research/football_day8_goal_conversion_2026-08-17_predictions.jsonl`
  - `research/football_research/football_day8_goal_conversion_2026-08-17_conversion_records.json`
  - `research/football_research/football_day8_goal_conversion_2026-08-17_corner_records.json`
  - `research/football_research/football_day8_goal_conversion_2026-08-17.md`
- `conclusion`: Day 8 is now closed for the currently loaded football scope because the downstream expected-goals layer is chronological, league-isolated, deterministic, versioned, and explicitly separated from the upstream GAP state and any market logic
- `interpretation`: this completes the bounded Day 8 model layer only. It does not yet provide the Day 9 Dixon-Coles scoreline object or any Day 10 market-derivation outputs

## EXP-012 - Day 9 Football Dixon-Coles Scoreline Layer

- `date`: 2026-08-17
- `scope`: consume the frozen Day 8 expected-goal outputs, fit a chronological Dixon-Coles scoreline layer, and persist the full `0..10 x 0..10` scoreline matrix without deriving markets
- `implementation`:
  - `code/src/sports_ai/experiments/football_day9_scoreline_model.py`
  - `code/src/sports_ai/experiments/football_day8_goal_conversion.py`
- `interface interpretation`:
  - Day 8 `expected_goals_home` and `expected_goals_away` are the canonical Day 9 Poisson intensities
  - Day 9 does not fit a second competing goals model
  - explicit Day 9 gamma is omitted to avoid double-counting the Day 8 venue effect
- `fixed scoreline contract`:
  - independent Poisson baseline over `0..10 x 0..10`
  - canonical Dixon-Coles tau correction on `0-0`, `1-0`, `0-1`, and `1-1`
  - fixed rho grid: `-0.20`, `-0.15`, `-0.13`, `-0.10`, `-0.05`, `0.00`, `0.05`
  - fixed xi grid: `0.0`, `0.001`
  - first evaluation season per competition defaults to `rho=0.0`, `xi=0.0` before any prior scoreline evidence exists
- `walk-forward design`:
  - preserve the inherited `2122` warm-up boundary from Day 7 and Day 8
  - fit rho using prior Day 8 expected-goal rows and observed score outcomes only
  - fit separately by competition
  - select xi for predictions by cumulative prior out-of-sample fold log likelihood, preferring `xi=0.0` on ties
- `variant count`:
  - `210` tested rho variants across competition, fold, xi, and rho candidate
- `real-archive result`:
  - prediction count: `9664`
  - final selected rho by competition: `ENG1=0.00`, `ENG2=0.00`, `ENG3=0.00`, `ENG4=-0.05`, `ESP1=0.00`
  - final selected xi by competition: `0.0` for every loaded competition
  - overall plain-Poisson log likelihood: `-27914.3931`
  - overall Dixon-Coles log likelihood: `-27917.2423`
  - overall log-likelihood delta: `-2.8491`
  - average draw-probability delta: `+0.001214`
  - average tail mass beyond `10`: about `0.00000219`
- `important interpretation`:
  - the implementation is compliant and deterministic, but the held-out evidence in the currently loaded scope does not show a general likelihood improvement from the fitted Dixon-Coles correction
  - draw probability rises on average overall, but not generally across all competitions or fixtures, so that discrepancy is recorded rather than forced away
- `determinism verified`:
  - repeated fresh real-store runs returned identical predictions
  - repeated fresh real-store runs returned identical rho-variant records
  - repeated fresh real-store runs returned identical fold records, diagnostics, final rho summaries, and time-decay summaries
- `artifacts`:
  - `research/football_research/football_day9_scoreline_model_2026-08-17_manifest.json`
  - `research/football_research/football_day9_scoreline_model_2026-08-17_predictions.jsonl`
  - `research/football_research/football_day9_scoreline_model_2026-08-17_rho_variants.json`
  - `research/football_research/football_day9_scoreline_model_2026-08-17_fold_records.json`
  - `research/football_research/football_day9_scoreline_model_2026-08-17.md`
- `conclusion`: Day 9 is now closed for the currently loaded football scope because the downstream scoreline layer is chronological, versioned, deterministic, explicit about tail handling, and preserved as a single goals-based probability object for Day 10
- `interpretation`: this completes the bounded Day 9 model layer only. It does not yet derive `1X2`, totals, BTTS, Asian handicap, corners prices, or any edge/recommendation outputs

## EXP-013 - Day 10 Football Market Pricing

- `date`: 2026-08-18
- `scope`: derive bounded Day 10 football fair prices from the frozen Day 9 scoreline matrix and the Day 8 corner-distribution object only
- `implementation`:
  - `code/src/sports_ai/experiments/football_day10_market_pricing.py`
  - `code/src/sports_ai/experiments/football_day9_scoreline_model.py`
  - `code/src/sports_ai/experiments/football_day8_goal_conversion.py`
- `fixed input contract`:
  - construct a sanitized pre-match pricing input
  - exclude realized goals, realized corners, bookmaker fields, edges, thresholds, staking, and decisions
  - carry deterministic scoreline-object and corner-object ids on every market row
- `goals-market contract`:
  - treat the persisted Day 9 scoreline matrix as the canonical goals object
  - derive `1X2`, `BTTS`, goals totals, and Asian handicap directly from that matrix
  - do not recompute Poisson probabilities from `expected_goals_home`, `expected_goals_away`, or `rho` inside Day 10
- `corners-market contract`:
  - treat Day 8 predicted home/away corners, selected corner distribution, and persisted dispersion as the canonical corners object
  - derive corner totals only from that object
  - use adaptive support with explicit residual tail mass rather than a fixed `0..10` truncation
- `settlement arithmetic`:
  - one generic settlement engine supports integer, half, and quarter lines
  - fair price uses `1 + L/W` with preserved full-win, half-win, push, half-loss, and full-loss probabilities
  - the same engine prices goals totals, corner totals, and Asian handicap
- `engineering verification line catalog`:
  - goals totals: `1.5`, `2.0`, `2.25`, `2.5`, `2.75`, `3.0`
  - Asian handicap using `home_handicap`: `-1.0`, `-0.75`, `-0.5`, `-0.25`, `0.0`, `0.25`, `0.5`, `0.75`, `1.0`
  - corner totals: `8.5`, `9.0`, `9.25`, `9.5`, `9.75`, `10.0`, `10.5`
- `real-archive result`:
  - sanitized pricing inputs: `9664`
  - market-price rows: `473536`
  - market-family rows:
    - `1x2`: `28992`
    - `btts`: `19328`
    - `goals_total`: `115968`
    - `asian_handicap`: `173952`
    - `corner_total`: `135296`
  - rows with scoreline source ids: `338240`
  - rows with corner source ids: `135296`
  - forbidden outcome / bookmaker / edge / stake fields found in market rows: `0`
- `determinism verified`:
  - repeated fresh runs returned identical sanitized pricing inputs
  - repeated fresh runs returned identical market-price rows
  - repeated fresh runs returned identical non-timestamp manifest content
- `artifacts`:
  - `research/football_research/football_day10_market_pricing_2026-08-18_manifest.json`
  - `research/football_research/football_day10_market_pricing_2026-08-18_pricing_inputs.jsonl`
  - `research/football_research/football_day10_market_pricing_2026-08-18_market_prices.jsonl`
  - `research/football_research/football_day10_market_pricing_2026-08-18.md`
- `conclusion`: Day 10 is now closed for the currently loaded football scope because the system can derive reproducible fair prices for the bounded goals and corner market families directly from frozen pre-match model objects without seeing bookmaker prices or realized outcomes
- `interpretation`: this completes the bounded Day 10 fair-price layer only. It does not yet resolve Day 11 edge semantics, live odds/sharp-reference integration, forward-production-fit readiness, or later football market families such as shots and shots on target

## EXP-014 - Day 10 Football Product Outputs

- `date`: 2026-08-18
- `scope`: package the closed Day 10 fair-pricing artifacts into manager-facing and software-facing product outputs without reopening the underlying model layer
- `implementation`:
  - `code/src/sports_ai/experiments/football_day10_product_outputs.py`
  - `code/tests/test_football_day10_product_outputs.py`
  - `code/src/sports_ai/tools/chatgpt_handoff_bundle.py`
- `product-output requirement`:
  - machine-readable artifact
  - reproducibility / lineage manifest
  - human-readable product/output report
  - compact `current/latest` artifact
- `verified outputs`:
  - `research/product_outputs/football/day10/FOOTBALL_DAY10_MODEL_PRICING_REPORT.md`
  - `research/product_outputs/football/day10/football_day10_model_prices.csv`
  - `research/product_outputs/football/day10/football_day10_model_prices.jsonl`
  - `research/product_outputs/football/day10/football_day10_product_manifest.json`
  - `research/product_outputs/football/latest/FOOTBALL_MODEL_PRICES.md`
  - `research/product_outputs/football/latest/football_model_prices.csv`
  - `research/product_outputs/football/latest/football_model_prices.jsonl`
  - `research/product_outputs/football/latest/football_pipeline_manifest.json`
- `real-output state`:
  - pricing-input count carried through: `9664`
  - market-price count carried through: `473536`
  - representative fixtures surfaced in the report: `Villarreal vs Ath Madrid`, `West Ham vs Leeds`, `Wycombe vs Rotherham`, `Tranmere vs Grimsby`, and `Wrexham vs Middlesbrough`
- `determinism / verification`:
  - focused export and bundle coverage passed
  - root `pytest -q` passed at `178 passed in 27.17s`
- `conclusion`: Day 10 now has both the required reproducible technical archive and the required current product-facing outputs
- `interpretation`: this closes the Day 10 presentation gap only. It does not begin Day 11A manager logic or change any Day 7 through Day 10 model assumptions

## EXP-015 - Day 11A Football Manager Core

- `date`: 2026-08-18
- `scope`: implement the provider-independent Day 11A football manager core on top of frozen Day 10
  fair-price rows plus supplied normalized historical/demo market snapshots
- `implementation`:
  - `code/src/sports_ai/experiments/football_day11_manager_core.py`
  - `code/src/sports_ai/experiments/football_day11_product_outputs.py`
- `key requirements closed`:
  - normalize supplied market snapshots without consuming bookmaker data inside Day 10
  - validate complete reference-market booksum per normalized market identity
  - persist detailed `power` and `shin` de-vig outputs while using `shin` as the configured final
    method
  - select `best_available_price` from supplied non-reference offers only
  - compute settlement-aware edge as `W * (o - 1) - L`
  - compute `minimum_acceptable_price = 1 + (L + T) / W`
  - apply competition-threshold mapping and bounded cup-rule scaffolding
  - emit full-slate `PRELIMINARY_CANDIDATE / NO_BET / REJECTED_RULE / MISSING_REQUIRED_MARKET_DATA / STALE_MARKET`
    decision records
  - finish with manager-facing Day 11 markdown/CSV/JSONL/latest artifacts
- `real run state`:
  - input label: `HISTORICAL / DEMONSTRATION MARKET INPUT`
  - current supplied in-repo market family coverage for the real run: `1X2`
  - matched decision rows: `28992`
  - normalized matched market snapshots: `37240`
  - reference markets de-vigged: `9664`
  - final de-vig method: `shin`
  - decision mix:
    - `PRELIMINARY_CANDIDATE`: `10299`
    - `NO_BET`: `18693`
    - `REJECTED_RULE`: `0`
    - `MISSING_REQUIRED_MARKET_DATA`: `0`
    - `STALE_MARKET`: `0`
- `important interpretation`:
  - `DAY11_MANAGER_CORE_READY = YES`
  - `LIVE_PAPER_PIPELINE_READY = NO`
- `product outputs`:
  - `research/product_outputs/football/day11/FOOTBALL_DAY11_MANAGER_REPORT.md`
  - `research/product_outputs/football/day11/football_day11_decisions.csv`
  - `research/product_outputs/football/day11/football_day11_decisions.jsonl`
  - `research/product_outputs/football/day11/football_day11_product_manifest.json`
  - `research/product_outputs/football/latest/FOOTBALL_DECISIONS.md`
  - `research/product_outputs/football/latest/football_decisions.csv`
  - `research/product_outputs/football/latest/football_decisions_sample.csv`
  - `research/product_outputs/football/latest/FOOTBALL_CURRENT_STATUS.md`
- `conclusion`: Day 11A is now closed for the currently loaded football scope because the system can
  derive deterministic, settlement-aware preliminary manager decisions from frozen Day 10 model
  prices and supplied market snapshots without seeing realized outcomes
- `interpretation`: this closes the bounded manager-core layer only. It does not claim live paper
  readiness, current-forward-fit readiness, Day 12 safeguards, or full later-market coverage

## Experiment: Football Day 12 safeguards - 2026-08-19

- `experiment_id`: `EXP-015`
- `date`: `2026-08-19`
- `hypothesis`: the frozen Day 11A preliminary candidates can be reconciled deterministically into
  a truthful Day 12 safeguard disposition set without reopening Day 10 or Day 11A arithmetic
- `data_version`: accepted Day 11A decisions plus Day 10 model-price archive and current in-repo
  archived closing `1X2` provenance
- `feature_version`: `football_day12_safeguards_v1`
- `model_version`: downstream safeguard layer only
- `sample_size`: `10299` incoming Day 11 preliminary candidates
- `metrics`:
  - baseline before coding: `187 passed in 40.06s`
  - post-implementation root suite: `195 passed in 33.22s`
  - final dispositions:
    - `4317` `EXPERIMENTAL_CANDIDATE`
    - `5982` `BLOCKED_MISSING_XG_DATA`
  - AH status:
    - `10299` `AH_NOT_APPLICABLE`
  - xG status:
    - `4317` `XG_NOT_APPLICABLE`
    - `5982` `XG_MISSING`
  - derivation status:
    - `10299` `DERIVATION_PASS`
  - survival percentage: `41.92%`
- `result`: bounded Day 12 safeguard logic is now implemented end to end, with real markdown/CSV/
  JSONL/latest outputs plus acceptance audit evidence
- `conclusion`: Day 12 is `PARTIAL`, not `COMPLETE`, because the code path exists but the required
  point-in-time-valid xG data path does not

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
