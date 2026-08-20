# Project Status

Last updated: Thursday, August 20, 2026

## Current phase

Thursday, August 20, 2026 implementation state is now updated through the football validation
build, historical performance benchmark, provider-independent current market contract, and
paper-trading foundation. The accepted football model path remains closed through Day 11A, with
Day 12 still implemented as an honest `PARTIAL` for the currently loaded football scope.

The current user-directed product milestone is now:

- `CURRENT PRODUCT MILESTONE = FOOTBALL VALIDATION + CURRENT MARKET/PAPER TRACKING FOUNDATION`
- `TENNIS DEVELOPMENT = PAUSED`

The repository remains the active migration workspace under the August 2026 PDF-first football and
tennis specification authority. For the currently loaded football slice, the accepted build is now
closed through:

- Day 7 GAP statistic lanes plus lambda fitting
- Day 8 expected-goals conversion and corner-distribution selection
- Day 9 Dixon-Coles scoreline layer
- Day 10 fair-price derivation
- Day 11A provider-independent manager core
- Day 12 football safeguards / veto foundation (`PARTIAL`)

The current bounded product boundary is now:

`frozen historical football outputs -> benchmark + audit -> current market-provider contract -> immutable paper ledger -> performance dashboard`

It is still not:

`live betting system ready`

## Football Validation Build

The active football milestone is now the historical validation and live-readiness foundation rather
than another dashboard redesign.

Real local verification on Thursday, August 20, 2026:

- full historical benchmark executed successfully from the frozen accepted football artifacts
- new dashboard `/performance` page and `/api/performance` endpoint added successfully
- current full suite after the validation build: `206 passed, 1 warning in 35.41s`

Real benchmark summary from the current repo snapshot:

- evaluation sample: `9664`
- `MODEL_VALIDATION = PROMISING`
- `PROFITABILITY_PROVEN = NO`
- `CLV_PROVEN = NO`
- `LIVE_EDGE_PROVEN = NO`
- `1X2` accuracy: `0.470509`
- `1X2` Brier: `0.625145`
- `1X2` log loss: `1.039517`
- goals MAE: `1.275611`
- corners MAE: `2.696198`
- average expected calibration error: `0.0157`
- strongest measured market: `GOALS_TOTAL`
- weakest measured market: `1X2`
- historical CLV availability: `UNAVAILABLE_HISTORICALLY`

The benchmark now ships under:

- `research/product_outputs/football/performance/FOOTBALL_MODEL_PERFORMANCE_REPORT.md`
- `research/product_outputs/football/performance/football_performance_summary.json`
- `research/product_outputs/football/performance/football_performance_by_market.csv`
- `research/product_outputs/football/performance/football_performance_by_competition.csv`
- `research/product_outputs/football/performance/football_performance_by_season.csv`
- `research/product_outputs/football/performance/football_calibration.csv`
- `research/product_outputs/football/performance/football_1x2_confusion_matrix.csv`
- `research/product_outputs/football/performance/FOOTBALL_MODEL_IMPROVEMENT_BACKLOG.md`

The new live-readiness foundation now also exists in code:

- provider-independent current market interface under `sports_ai.market_data`
- append-oriented current market snapshot storage
- immutable football paper-trading ledger foundation with deterministic settlement reuse
- dashboard performance surface with readiness/status reporting

The key verified runtime truth is now:

- `CURRENT_ODDS_PROVIDER = NOT_CONFIGURED`
- `CURRENT_MARKET_DECISIONS = BLOCKED`
- `PAPER_TRADING_LEDGER_READY = YES`
- `CURRENT_PAPER_TRACKING_ACTIVE = NO`
- `CLV_TRACKING_READY = YES`

This means the football model can now be benchmarked and audited honestly, and the repo is ready
to ingest real current market snapshots once a provider is connected, but it still cannot claim
live paper profitability or live CLV evidence.

## Football Dashboard V1.2

Dashboard V1.2 is now the current accepted operational dashboard state for the loaded football
scope.

It preserves the accepted football math stack and refreshes the runtime around:

- `football-data.org` as the default current fixture source
- a dedicated `GET /v4/matches` provider adapter with bounded caching
- a dedicated `Status` page at `/status`
- denser watchlist-style dashboard, matches, predictions, candidates, and match-detail pages
- next-match handling that includes the nearest future fixtures rather than only next-day rows

Real local verification on Wednesday, August 19, 2026:

- dashboard app booted successfully under Uvicorn
- `GET /status` returned `200`
- `GET /api/system/status` returned:
  - `fixture_feed.provider = football-data.org`
  - `fixture_feed.state = NOT_CONFIGURED`
  - `prediction_engine.state = READY`
- current full suite after V1.2 refresh: `203 passed in 35.13s`

The key verified runtime truth is now:

- the dashboard default provider is no longer the older API-Football path
- the verified local environment on Wednesday, August 19, 2026 does not currently set
  `SPORTS_AI_FOOTBALL_DATA_TOKEN`
- therefore the dashboard now reports `fixture_feed.state = NOT_CONFIGURED` honestly instead of
  fabricating fixtures or pretending the new source is connected

This means Dashboard V1.2 closes the requested provider/UI refresh build honestly, but it still
does not close the "first real current football-data.org fixture to prediction" acceptance target
until the token is configured.

## Football Dashboard V1.1

Dashboard V1.1 is now the current accepted operational dashboard state for the loaded football
scope.

It keeps the V1 product boundary, but hardens the runtime around:

- provider-truth diagnostics instead of silent empty-slate handling
- explicit fixture-feed degraded state
- `GET /api/fixtures/next`
- `GET /api/predictions`
- `POST /api/fixtures/refresh`
- manual refresh cooldown control
- prediction lock metadata:
  - `prediction_locked_at`
  - `prediction_version`
- the rule that ordinary polling must not create a first-ever prediction after kickoff

Real local verification on Wednesday, August 19, 2026:

- dashboard server booted successfully on `http://127.0.0.1:8008`
- `GET /` returned `200`
- `GET /matches` returned `200`
- `GET /predictions` returned `200`
- `GET /candidates` returned `200`
- `GET /api/system/status` returned `200`
- `GET /api/fixtures/today` returned `200`
- `GET /api/fixtures/upcoming` returned `200`
- `GET /api/fixtures/next` returned `200`
- current full suite after V1.1 hardening: `202 passed in 29.50s`

Real runtime state from the dashboard now reports:

- `fixture_feed.state = DEGRADED`
- `prediction_engine.state = READY`
- `current_model_snapshot.state = READY`
- `current_fixture_feed_ready = NO`
- `current_prediction_pipeline_ready = YES`
- `current_market_decision_ready = NO`
- `live_paper_pipeline_ready = NO`

The key verified runtime truth is now:

- the configured API-Football provider is authenticated
- the current season identifier is correctly `2026`
- the configured plan blocks current-season fixture rows for the scoped leagues
- therefore the earlier Aug 19-26 empty slate was not a clean "no fixtures exist" result
- it was a provider-access limitation that V1 had not surfaced honestly

This means Dashboard V1.1 improves operational honesty and diagnostics, but it does not yet close
the "first real current prediction" acceptance target under the current provider plan.

## Operational Football Dashboard V1

The dashboard milestone is now implemented locally in the repo as a real FastAPI application with:

- a normalized current-fixture provider abstraction
- an API-Football adapter using the configured project credentials
- SQLite-backed dashboard persistence for fixtures, prediction versions, and team-resolution traces
- a current forward football snapshot builder that replays the loaded historical archive into current
  GAP state and fits current dashboard-side Day 8 / Day 9 parameters from completed data
- server-side frozen pre-match football prediction generation for eligible fixtures
- Day 10 fair-price derivation surfaced in match cards and match detail
- HTML pages plus read APIs for dashboard, matches, predictions, candidates, and system status
- lightweight partial refresh for the primary pages without full-page reloads

Real local verification on Wednesday, August 19, 2026:

- dashboard server booted successfully on `http://127.0.0.1:8008`
- `GET /` returned `200`
- `GET /api/system/status` returned:
  - `fixture_feed.state = CONNECTED`
  - `prediction_engine.state = READY`
  - `current_model_snapshot.state = READY`
  - `dashboard_ui_ready = YES`
  - `current_fixture_feed_ready = YES`
  - `current_prediction_pipeline_ready = YES`
  - `current_market_decision_ready = NO`
  - `live_paper_pipeline_ready = NO`
- the real configured API-Football feed returned an empty in-scope slate for `ENG1`, `ENG2`,
  `ENG3`, `ENG4`, and `ESP1` across Wednesday, August 19, 2026 through Wednesday, August 26, 2026:
  - live fixtures: `0`
  - today's fixtures: `0`
  - upcoming fixtures inside the current horizon: `0`

This means the dashboard is operational and connected without fabricating matches, but the current
runtime did not have a supported real fixture available to display as a generated live example at
verification time.

## Verified Day 12 state

- real Day 12 safeguard run completed from frozen repo-backed Day 11A preliminary candidates
- incoming Day 11A preliminary candidates: `10299`
- final Day 12 disposition mix:
  - `4317` `EXPERIMENTAL_CANDIDATE`
  - `5982` `BLOCKED_MISSING_XG_DATA`
  - `0` `REJECTED_BY_AH_RULE`
  - `0` `REJECTED_BY_XG_VETO`
  - `0` `REJECTED_BY_MULTIPLE_VETOES`
  - `0` `DERIVATION_ERROR`
  - `0` `DAY12_REVIEW_REQUIRED`
- AH safeguard status mix:
  - `10299` `AH_NOT_APPLICABLE`
- xG safeguard status mix:
  - `4317` `XG_NOT_APPLICABLE`
  - `5982` `XG_MISSING`
- derivation consistency status mix:
  - `10299` `DERIVATION_PASS`
- survival percentage: `41.92%`
- `DAY12 STATUS = PARTIAL`
- `FOOTBALL_LOGICAL_PIPELINE_READY = NO`
- `LIVE_PAPER_PIPELINE_READY = NO`
- `TENNIS AUTHORIZED = NO`

The real Day 12 run is still explicitly:

- `HISTORICAL / DEMONSTRATION MARKET INPUT`
- `NOT LIVE BETTING OUTPUT`
- `NOT REAL-MONEY RECOMMENDATIONS`
- current real supplied market family coverage in-repo: archived closing `1X2` only

Day 12 is partial because the repo still does not contain a connected point-in-time-valid xG input
path for the competitions where the football spec requires that check. The safeguard contract,
blocked-state handling, and reporting are implemented; the data dependency is not fabricated.

## Verified Day 11A state

- real Day 11A historical/demo manager run completed from frozen repo-backed inputs
- `28992` Day 11 decision records
- decision mix:
  - `10299` `PRELIMINARY_CANDIDATE`
  - `18693` `NO_BET`
  - `0` `REJECTED_RULE`
  - `0` `MISSING_REQUIRED_MARKET_DATA`
  - `0` `STALE_MARKET`
- matched supplied market selections: `28992`
- normalized matched market snapshots persisted: `37240`
- reference markets devigged: `9664`
- configured final de-vig method: `shin`
- `DAY11_MANAGER_CORE_READY = YES`
- `LIVE_PAPER_PIPELINE_READY = NO`

The in-repo supplied market input used for the real Day 11A run is explicitly:

- `HISTORICAL / DEMONSTRATION MARKET INPUT`
- current real supplied market family coverage in-repo: `1X2`
- the archived closing `1X2` feed is valid for historical/demo manager arithmetic but is not a
  genuinely timestamped original live-capture market snapshot

That means the manager layer is now implemented and auditable, but the current live/provider and
current-forward-fit dependencies remain unresolved.

## Current outputs

Day 11A now ships both technical and manager-facing artifacts:

- `research/football_research/football_day11_manager_2026-08-18_normalized_market_snapshots.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_joined_rows.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_devig_outputs.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_manifest.json`
- `research/product_outputs/football/day11/FOOTBALL_DAY11_MANAGER_REPORT.md`
- `research/product_outputs/football/day11/football_day11_decisions.csv`
- `research/product_outputs/football/day11/football_day11_decisions.jsonl`
- `research/product_outputs/football/day11/football_day11_product_manifest.json`
- `research/product_outputs/football/latest/FOOTBALL_DECISIONS.md`
- `research/product_outputs/football/latest/football_decisions.csv`
- `research/product_outputs/football/latest/football_decisions_sample.csv`
- `research/product_outputs/football/latest/FOOTBALL_CURRENT_STATUS.md`

Day 12 now ships both technical and manager-facing artifacts:

- `research/football_research/football_day12_safeguards_2026-08-19_candidate_records.jsonl`
- `research/football_research/football_day12_safeguards_2026-08-19_manifest.json`
- `research/product_outputs/football/day12/FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md`
- `research/product_outputs/football/day12/football_day12_candidates.csv`
- `research/product_outputs/football/day12/football_day12_candidates.jsonl`
- `research/product_outputs/football/day12/football_day12_product_manifest.json`
- `research/product_outputs/football/latest/FOOTBALL_EXPERIMENTAL_CANDIDATES.md`
- `research/product_outputs/football/latest/football_experimental_candidates.csv`
- `research/product_outputs/football/latest/football_experimental_candidates_sample.csv`
- `research/product_outputs/football/latest/FOOTBALL_CURRENT_STATUS.md`
- `research/spec_migration/FOOTBALL_DAY12_ACCEPTANCE_AUDIT_2026-08-19.md`

Day 10 product outputs remain available under both:

- `research/product_outputs/football/day10/`
- `research/product_outputs/football/latest/`

The `latest` Day 10 copies are now stored as hard links to avoid an unnecessary duplicate multi-GB
 storage hit inside the local workspace.

Performance outputs now also ship under:

- `research/product_outputs/football/performance/FOOTBALL_MODEL_PERFORMANCE_REPORT.md`
- `research/product_outputs/football/performance/football_performance_summary.json`
- `research/product_outputs/football/performance/football_performance_by_market.csv`
- `research/product_outputs/football/performance/football_performance_by_competition.csv`
- `research/product_outputs/football/performance/football_performance_by_season.csv`
- `research/product_outputs/football/performance/football_calibration.csv`
- `research/product_outputs/football/performance/football_1x2_confusion_matrix.csv`
- `research/product_outputs/football/performance/FOOTBALL_MODEL_IMPROVEMENT_BACKLOG.md`

## Repository state

- Day 10 model archive remains frozen and unreopened:
  - `9664` sanitized pricing inputs
  - `473536` fair-price rows
- Day 11A consumes the persisted Day 10 settlement-aware fields directly and does not rebuild model
  prices from expected goals or score matrices
- verified root suite before Day 12 coding: `187 passed in 40.06s`
- verified root suite after Day 12 implementation: `195 passed in 33.22s`
- verified root suite after Operational Football Dashboard V1 implementation: `200 passed in 31.70s`
- verified root suite after Football Dashboard V1.1 runtime validation hardening: `202 passed in 29.50s`
- verified root suite after Football Dashboard V1.2 football-data.org refresh: `203 passed in 35.13s`
- verified root suite after Football Validation Build: `206 passed, 1 warning in 35.41s`
- root test route remains `pytest -q` from repo root via `pytest.ini`
- new Day 11A implementation modules:
  - `code/src/sports_ai/experiments/football_day11_manager_core.py`
  - `code/src/sports_ai/experiments/football_day11_product_outputs.py`
- new Day 12 implementation modules:
  - `code/src/sports_ai/experiments/football_day12_safeguards.py`
  - `code/src/sports_ai/experiments/football_day12_product_outputs.py`
- new Day 11A test coverage:
  - `code/tests/test_football_day11_manager_core.py`
  - `code/tests/test_football_day11_product_outputs.py`
- new Day 12 test coverage:
  - `code/tests/test_football_day12_safeguards.py`
- new validation-build implementation modules:
  - `code/src/sports_ai/experiments/football_model_performance.py`
  - `code/src/sports_ai/market_data/providers.py`
  - `code/src/sports_ai/storage/football_paper_trading.py`
- new validation-build test coverage:
  - `code/tests/test_football_model_performance.py`
  - `code/tests/test_football_paper_trading.py`
- current repo shape:
  - `72` Python modules under `code/src/sports_ai`
  - `32` `test_*.py` files under `code/tests`
  - `196` explicit `def test_...` functions

## Locked development sequence

1. football historical validation build complete
2. connect a real current odds provider through the new provider-independent market-data contract
3. capture genuine current market snapshots and activate current paper-decision persistence
4. resolve current forward-production football parameter snapshots for present-tense paper use
5. resolve point-in-time-valid xG dependency for the competitions where Day 12 requires it
6. freeze the football logical pipeline only after live market and xG blockers are resolved
7. tennis remains paused until football is explicitly re-authorized as complete enough to branch

Do not reopen accepted Days 7 through 11A unless a genuine correctness issue is discovered.

## Active constraints

- the supplied PDFs and companion Markdown files are not identical, so requirement conflicts must
  still be resolved through the PDF-first authority order
- the currently loaded football archive still covers only `ENG1`, `ENG2`, `ENG3`, `ENG4`, and
  `ESP1`, not the full football-spec scope
- no resolved live sharp-reference provider is connected yet
- no resolved current odds provider is connected yet, so the new market-provider contract remains
  unconfigured in production terms
- no approved production odds-snapshot runtime is in place yet
- the current real historical Day 11A market input is archived closing `1X2`, not a genuine
  point-in-time original market capture
- the historical benchmark now measures calibration and market-level performance honestly, but it
  still cannot prove decision-time ROI or CLV from the archived closing-snapshot-only source mix
- current Day 8 conversion coefficients, Day 8 corner parameters, and Day 9 rho are valid
  walk-forward historical artifacts, but they are not automatically a current forward-production fit
  for 2026-27 paper predictions
- Day 12 safeguard logic is implemented, but point-in-time-valid xG input still does not exist in
  the current repo run-path for the competitions where the football spec requires that check; this
  is why Day 12 remains `PARTIAL`
- the repository/runtime/database split between football and tennis is still pending
- no final production PostgreSQL architecture should be assumed yet

## Immediate next actions

1. keep the accepted Day 7 through Day 12 model mathematics frozen unless a genuine correctness
   issue is found
2. connect a current odds provider through the new normalized market-data contract
3. activate genuine current market snapshot capture and immutable paper-trade entry storage
4. resolve the point-in-time-valid xG dependency for the competitions where the football spec
   requires that safeguard
5. resolve current forward-production Day 8 and Day 9 parameter snapshot readiness for 2026-27
   paper output
6. preserve the distinction between connected current data plumbing and a fully ready live paper
   betting pipeline
7. do not begin tennis, staking, Kelly, or production deployment until football status is
   explicitly re-authorized

## Bottom line

For the currently loaded football scope, the accepted football model stack remains closed through
Day 11A and the repo now has a real validation and paper-tracking foundation on top of it:

- frozen Day 10 fair prices are consumed as immutable model authority
- supplied market snapshots are normalized and validated
- reference markets are booksummed and de-vigged
- best available prices are selected per normalized selection
- settlement-aware edge and minimum acceptable price are computed from Day 10 `W` and `L`
- thresholds are applied deterministically
- full-slate preliminary candidate / no-bet decisions are persisted and reported
- every Day 11 preliminary candidate now receives a deterministic Day 12 safeguard disposition
- missing xG is surfaced honestly as a blocked state instead of being silently bypassed
- the accepted historical football stack now has a formal performance benchmark and manager-facing
  performance report
- current market provider, snapshot storage, and paper-ledger contracts now exist without
  fabricating live odds
- the dashboard now exposes a dedicated `/performance` surface for benchmark and paper-tracking
  readiness

The broader August 2026 build is still not done. Point-in-time-valid xG coverage, the football
logical-pipeline freeze, full archive scope, live odds connectivity, current forward-fit football
paper readiness, tennis implementation, and the final football/tennis split all remain pending.
