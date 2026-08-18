# Football GAP Shots Walk-Forward (Fitted Lambda)

Generated at: `2026-08-17T16:37:44+00:00`
Experiment/version: `EXP-010` / `football_gap_shots_walkforward_fitted_v1`

## Run Summary

- competitions processed: `ENG1, ENG2, ENG3, ENG4, ESP1`
- seasons processed: `2122, 2223, 2324, 2425, 2526`
- statistic: `shots`
- learning rate mode: `0.1` (`WALK_FORWARD_FITTED`)
- baseline policy: `warmup_first_ready_season_league_venue_average_v1`
- as_of policy: `historical_prediction_as_of_fixture_kickoff_v1`
- matches processed: `12080`
- predictions produced: `12080`
- evaluation predictions: `9664`
- events skipped: `0`

## Policies

- warm-up policy: The earliest readiness-approved season per competition is treated as a warm-up season. Predictions are exported for reproducibility, but evaluation diagnostics exclude warm-up rows.
- season-transition policy: Carry GAP state forward across seasons within the same competition. Do not reset at season boundaries. Teams newly appearing in a competition start from the competition baseline with zero current-competition matches observed.
- duplicate-event policy: Reject duplicate observations inside GapRatingsEngine by (competition_id, statistic, event_id) so the same event cannot move ratings twice.
- as_of policy: Prediction-row as_of is the latest source-backed pre-match timestamp the replayed prediction is allowed to depend on. Because the historical source exposes fixture date plus kickoff time but no finer observation timestamp for the football-process inputs used here, persist as_of as the fixture kickoff timestamp when kickoff_time is present, otherwise the conservative day-start timestamp match_dateT00:00:00. This preserves the rule that historical pre-match as_of never exceeds kickoff.

## Learning Rates by Competition

| Competition | Learning Rate |
| --- | ---: |
| ENG1 | 0.1000 |
| ENG2 | 0.1000 |
| ENG3 | 0.1000 |
| ENG4 | 0.1000 |
| ESP1 | 0.1000 |

## Diagnostics by Competition

| Competition | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ENG1 | 1520 | 3.8678 | 3.5861 | 3.7269 | 0.1202 | -0.0762 |
| ENG2 | 2208 | 3.7293 | 3.3738 | 3.5515 | 0.2954 | -0.0325 |
| ENG3 | 2208 | 3.6467 | 3.3938 | 3.5203 | 0.0744 | -0.1659 |
| ENG4 | 2208 | 3.6735 | 3.3662 | 3.5198 | 0.0925 | -0.1381 |
| ESP1 | 1520 | 3.7019 | 3.3994 | 3.5506 | 0.2290 | -0.0013 |

## Diagnostics by Season

| Season | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2223 | 2416 | 3.5545 | 3.3588 | 3.4566 | 0.1858 | -0.2737 |
| 2324 | 2416 | 3.8792 | 3.5166 | 3.6979 | 0.4437 | 0.1485 |
| 2425 | 2416 | 3.7486 | 3.3829 | 3.5658 | -0.1773 | -0.2797 |
| 2526 | 2416 | 3.6782 | 3.3978 | 3.5380 | 0.1901 | 0.0487 |

## Notes

- Diagnostics are evaluation-only and exclude warm-up-season rows.
- This experiment uses only football-process statistics from the research store.
- No betting simulation, market pricing, or goal conversion is included.