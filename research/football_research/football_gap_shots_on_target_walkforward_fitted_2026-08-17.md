# Football GAP Shots on Target Walk-Forward (Fitted Lambda)

Generated at: `2026-08-17T16:37:45+00:00`
Experiment/version: `EXP-010` / `football_gap_shots_on_target_walkforward_fitted_v1`

## Run Summary

- competitions processed: `ENG1, ENG2, ENG3, ENG4, ESP1`
- seasons processed: `2122, 2223, 2324, 2425, 2526`
- statistic: `shots_on_target`
- learning rate mode: `0.05` (`WALK_FORWARD_FITTED`)
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
| ENG1 | 0.0500 |
| ENG2 | 0.0500 |
| ENG3 | 0.0500 |
| ENG4 | 0.0500 |
| ESP1 | 0.0500 |

## Diagnostics by Competition

| Competition | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ENG1 | 1520 | 1.8866 | 1.7030 | 1.7948 | 0.0480 | -0.0503 |
| ENG2 | 2208 | 1.7827 | 1.6262 | 1.7045 | 0.1130 | -0.0634 |
| ENG3 | 2208 | 1.8165 | 1.7065 | 1.7615 | 0.0336 | -0.1428 |
| ENG4 | 2208 | 1.8221 | 1.6738 | 1.7480 | 0.0786 | -0.1109 |
| ESP1 | 1520 | 1.8026 | 1.6430 | 1.7228 | 0.2205 | -0.0209 |

## Diagnostics by Season

| Season | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2223 | 2416 | 1.7944 | 1.6490 | 1.7217 | 0.1180 | -0.2311 |
| 2324 | 2416 | 1.8856 | 1.6996 | 1.7926 | 0.3265 | 0.0959 |
| 2425 | 2416 | 1.7856 | 1.6467 | 1.7161 | -0.0814 | -0.1485 |
| 2526 | 2416 | 1.8100 | 1.6853 | 1.7476 | 0.0117 | -0.0510 |

## Notes

- Diagnostics are evaluation-only and exclude warm-up-season rows.
- This experiment uses only football-process statistics from the research store.
- No betting simulation, market pricing, or goal conversion is included.