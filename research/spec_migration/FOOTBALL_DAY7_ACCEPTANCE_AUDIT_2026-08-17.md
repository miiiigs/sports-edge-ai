# Football Day 7 Acceptance Audit

Date: Monday, August 17, 2026

Authoritative basis:

- `initial_data/00-developer-brief.pdf`
- `initial_data/02-football-build-spec.pdf`
- `initial_data/03-thirty-day-plan.pdf`

Scope of this audit:

- close Day 7 for the currently loaded football archive scope only
- do not authorize Day 8 unless all nine checks pass

## Fixed Day 7 fitting configuration

- lambda grid: `0.02`, `0.05`, `0.10`
- variant count: `45`
- holdout policy: reserve `2526` as the latest approved season in each loaded competition during lambda selection
- fitted replay artifacts:
  - `research/football_research/football_gap_lambda_fitting_2026-08-17.json`
  - `research/football_research/football_gap_lambda_fitting_2026-08-17.md`
  - `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17_manifest.json`
  - `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17_manifest.json`
  - `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17_manifest.json`

## Explicit `as_of` policy

Historical pre-match prediction rows persist:

- fixture kickoff timestamp when `kickoff_time` exists
- otherwise `match_dateT00:00:00`

This is conservative and deterministic because the current historical source exposes fixture date
plus kickoff time but not a finer pre-match observation timestamp for the process-stat inputs used
here. The persisted prediction-row `as_of` therefore never exceeds fixture kickoff.

## Acceptance table

| Check | Status | Code / artifact evidence | Test evidence |
| --- | --- | --- | --- |
| shots independent | `PASS` | `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`; `research/football_research/football_gap_shots_walkforward_fitted_2026-08-17_manifest.json` | `code/tests/test_football_gap_shots_walkforward.py` |
| shots_on_target independent | `PASS` | `code/src/sports_ai/experiments/football_gap_shots_on_target_walkforward.py`; `research/football_research/football_gap_shots_on_target_walkforward_fitted_2026-08-17_manifest.json` | `code/tests/test_football_gap_shots_on_target_walkforward.py` |
| corners independent | `PASS` | `code/src/sports_ai/experiments/football_gap_corners_walkforward.py`; `research/football_research/football_gap_corners_walkforward_fitted_2026-08-17_manifest.json` | `code/tests/test_football_gap_corners_walkforward.py` |
| lambda fitted per league/statistic where sufficient data exists | `PASS` | `code/src/sports_ai/experiments/football_gap_lambda_fitting.py`; `research/football_research/football_gap_lambda_fitting_2026-08-17.json` records selected lambda for each loaded competition/statistic | `code/tests/test_football_gap_lambda_fitting.py` |
| every tested variant counted/logged | `PASS` | `research/football_research/football_gap_lambda_fitting_2026-08-17.json` logs `variant_count = 45` and every competition/statistic/lambda variant | `code/tests/test_football_gap_lambda_fitting.py::test_lambda_fitting_counts_every_variant` |
| persisted output explicitly carries `as_of` | `PASS` | fitted predictions JSONL artifacts carry explicit `as_of`; generic replay shell documents the policy in `code/src/sports_ai/experiments/football_gap_shots_walkforward.py` | `code/tests/test_football_gap_shots_walkforward.py`, `code/tests/test_football_gap_shots_on_target_walkforward.py`, `code/tests/test_football_gap_corners_walkforward.py` |
| persisted output carries `model_version` | `PASS` | fitted manifests, prediction rows, and final-state artifacts carry explicit `model_version` for all three statistic lanes | `code/tests/test_football_gap_shots_walkforward.py`, `code/tests/test_football_gap_shots_on_target_walkforward.py`, `code/tests/test_football_gap_corners_walkforward.py`, `code/tests/test_football_gap_lambda_fitting.py` |
| chronology / replay deterministic | `PASS` | repeated real-store fitting runs returned identical variant and selection records; repeated fitted replay runs returned identical predictions and state for `shots`, `shots_on_target`, and `corners` | replay determinism tests in the three walk-forward test files plus deterministic fitting tests in `code/tests/test_football_gap_lambda_fitting.py` |
| goals cannot enter GAP ratings | `PASS` | `code/src/sports_ai/agents/football/gap_ratings.py` rejects goals-like statistics at the contract boundary | `code/tests/test_gap_ratings.py`; goals-statistic rejection tests in the three walk-forward test files |

## Selected lambdas

| Statistic | Competition | Selected lambda |
| --- | --- | ---: |
| shots | `ENG1` | `0.10` |
| shots | `ENG2` | `0.10` |
| shots | `ENG3` | `0.10` |
| shots | `ENG4` | `0.10` |
| shots | `ESP1` | `0.10` |
| shots_on_target | `ENG1` | `0.05` |
| shots_on_target | `ENG2` | `0.05` |
| shots_on_target | `ENG3` | `0.05` |
| shots_on_target | `ENG4` | `0.05` |
| shots_on_target | `ESP1` | `0.05` |
| corners | `ENG1` | `0.05` |
| corners | `ENG2` | `0.05` |
| corners | `ENG3` | `0.05` |
| corners | `ENG4` | `0.05` |
| corners | `ESP1` | `0.05` |

## Day 7 result

All nine acceptance checks pass for the currently loaded football archive scope.

Day 7 status: `COMPLETE`

Day 8 authorization: `YES`

Important limitation:

- this does not remove the broader project blockers around full football archive scope, sharp-reference provider resolution, shared-runtime architecture, or downstream Day 8 through Day 10 implementation work

Day 10-11 paper-candidate target:

- still aggressive
- not yet realistic to describe as safe
- remaining dependencies still include Day 8 conversion/distributions, Day 9 scoreline work, Day 10 market derivation, usable current odds reference, and pre-market prediction locking in the final live architecture
