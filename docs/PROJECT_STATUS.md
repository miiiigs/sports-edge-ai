# Project Status

Last updated: Friday, August 14, 2026

## Current phase

Football trial-pipeline hardening on top of the recovered Pack-style codebase. The project is no longer documentation-only. It now has runnable Python code, tested persistence, football ingestion, a research database, a blind football baseline model, a paper/shadow ledger, and an operator-facing review interface.

## Current operating goal

Build an honest football paper/shadow trial loop for Saturday, August 15, 2026.

That means:

- use pre-event sports data only for the blind model;
- persist the prediction before market comparison;
- capture current market prices with source and timestamp;
- apply conservative opening-week filters;
- publish up to two paper/shadow trial candidates, never force exactly two;
- append close and settlement later for grading.

## Completed

- recovered the Developer Pack starter structure into a working Python project
- expanded the automated test suite to `65` passing tests
- built a local football research store with separated sports rows and market quote rows
- loaded historical football seasons `2122`, `2223`, `2324`, `2425`, and `2526` for:
  - `E0` Premier League
  - `E1` Championship
  - `E2` League One
  - `E3` League Two
  - `SP1` La Liga
- current football research store totals:
  - `12,080` football events
  - `46,904` football market quote rows
  - `25` ingest runs / coverage snapshots
- built the first blind football Poisson `1X2` baseline using prior results only
- built the football watchlist pipeline for upcoming fixtures
- added fixture-source fallback between free CSV and `football-data.org`
- added persistent prediction-lock and market-lock storage for the football watchlist
- added append-only market snapshot storage for current observed prices
- changed live football watchlist outputs to `PAPER_SHADOW_CANDIDATE`
- changed the historical close-vs-close football lane to `RESEARCH_SIGNAL`
- built the paper/shadow ledger, close/settlement append flow, static dashboard, and persistent review app
- upgraded the historical Poisson replay so it now uses prior-season context rather than starting the target season from an empty state
- verified the current Poisson replay baseline on `2526` against the bookmaker benchmark:
  - `2,284` scored predictions
  - model multiclass log loss about `1.04635`
  - market multiclass log loss about `1.01404`
  - `754` candidates at the current baseline rules
  - candidate ROI about `-14.16%`
- completed the first proper Track B split experiment:
  - train window `2122-2324`
  - validation window `2425`
  - final test window `2526`
  - compared `football_poisson_1x2_v1` vs recency-weighted `football_poisson_1x2_v2`
  - rejected `v2` because it underperformed `v1` on both validation and test
  - validation:
    - `v1` log loss about `1.04225`
    - `v2` log loss about `1.07688`
  - final test:
    - `v1` log loss about `1.05684`
    - `v2` log loss about `1.07613`
  - candidate ROI stayed negative for both models, so no betting-strategy claim is justified
- corrected the Track B research protocol so iterative development no longer exposes `2526` test metrics by default:
  - development mode now runs validation only
  - finalization mode is a separate command for one selected model on one held-out season
  - Track B outputs now separate:
    - probability-model evaluation
    - betting-policy diagnostics
  - calibration buckets are now reported by selection
- implemented `football_poisson_1x2_v3` as an opponent-adjusted, regularized team-strength Poisson model
- ran the first validation-only `v3` sweep on `2425`
- found a promotable `v3` validation configuration at `strength_regularization=0.03`:
  - `v1` validation log loss about `1.04225`
  - `v3` validation log loss about `1.04067`
  - `v1` validation Brier about `0.62716`
  - `v3` validation Brier about `0.62615`
  - `v3` passed the current promotion checks versus `v1` on:
    - overall log loss
    - overall Brier
    - no major league degradation
    - no major home/draw/away degradation
- saved the validation-only development artifact at `research/football_research/track_b_development_v3_reg003_2026-08-14.json`
- completed controlled Track B finalization for frozen `v1` versus selected `v3` on `2526`:
  - holdout status: previously observed holdout, not pristine
  - `v1` finalization log loss about `1.05684`
  - `v3` finalization log loss about `1.04542`
  - `v1` finalization Brier about `0.63648`
  - `v3` finalization Brier about `0.62901`
  - `v3` improved log loss by about `0.01142`
  - `v3` improved Brier by about `0.00747`
  - no major league collapse observed
  - no major home/draw/away diagnostic collapse observed
  - classification: `PROMOTE`
- saved the finalization artifacts:
  - `research/football_research/track_b_finalization_v1_2026-08-14.json`
  - `research/football_research/track_b_finalization_v3_reg003_2026-08-14.json`
  - `research/football_research/track_b_finalization_summary_v1_vs_v3_2026-08-14.json`
- implemented `football_poisson_1x2_v4` as a Dixon-Coles low-score correction layered on top of frozen `v3` opponent-adjusted strengths
- ran a validation-only `v4` rho sweep on `2425` using the frozen `v3` strength configuration:
  - best observed development-time parameter: `dixon_coles_rho = -0.05`
  - sweep artifact saved at `research/football_research/track_b_v4_rho_sweep_2026-08-14.json`
- ran frozen `v3` versus selected `v4` on validation only:
  - `v3` validation log loss about `1.04067`
  - `v4` validation log loss about `1.04011`
  - `v3` validation Brier about `0.62615`
  - `v4` validation Brier about `0.62594`
  - `v4` passed the current promotion checks versus `v3` on validation:
    - better overall log loss
    - Brier no worse
    - no major league degradation
    - no major home/draw/away degradation
  - development artifact saved at `research/football_research/track_b_development_v4_rho_neg005_2026-08-14.json`

## In progress

- verifying exact August 15, 2026 football fixtures across the target leagues
- evaluating current bookmaker-price availability for those fixtures
- tightening the temporary opening-week risk gate
- preparing the review interface to make watchlist results easier for operator/client use
- upgraded the live football watchlist to support promoted `poisson_1x2_v3` with `strength_regularization=0.03`
- added a persistent live watchlist action and last-run diagnostics state into the review app
- preparing controlled finalization for selected `v4` against frozen `v3` without reopening any tuning loop on future data
- continuing MLB reverse-engineering as a secondary workstream, not the next-24-hour priority

## Current constraints

- no live PostgreSQL security / role-enforced firewall yet
- no API-Football adapter implemented yet
- current price coverage is still source-dependent and incomplete
- opening-week football has unusual uncertainty because last-season priors are stale for some clubs
- the promoted `v3` live watchlist is now wired into the persistent review app, but the Saturday, August 15, 2026 auto-run still returned `fixtures_discovered = 0`
- the current watchlist still cannot produce live August 15 cards until fixture discovery succeeds with a dependable source
- the promoted Track B model still remains weaker than the bookmaker market on historical forecasting
- the `2526` season has already been viewed historically, so it should not be treated as a perfectly untouched future test set
- `v4` has only cleared validation so far and still needs controlled finalization before any promotion decision

## Immediate next actions

1. fix dependable fixture acquisition for Saturday, August 15, 2026 target leagues or use a manual fixture import path inside the review app
2. capture current prices with bookmaker, source, and timestamp
3. run the locked football watchlist with promoted `v3` and all `1X2` selections available
4. keep promoted `v3` frozen as the new Track B historical benchmark candidate
5. run controlled finalization for selected `v4` versus frozen `v3`
6. continue comparing every new model version against `football_poisson_1x2_v1` and promoted `v3` using validation-first forecasting metrics before any ROI interpretation
7. append close and settlement later for CLV and outcome review

## Current bottom line

The project is now a real and tested research system with a meaningful football benchmark and a tighter Track B protocol. `v2` is rejected. `v3` is the promoted benchmark. `v4` is now the next promising experiment because it improved validation metrics against frozen `v3`, but it has not yet been finalized and should not be treated as promoted. This is still not evidence of validated live picks, and Track A should not be changed casually from this alone. Live paper/shadow operation continues only if fixture and market capture are reliable.
