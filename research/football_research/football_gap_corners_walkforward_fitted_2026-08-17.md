# Football GAP Corners Walk-Forward (Fitted Lambda)

Generated at: `2026-08-17T16:37:43+00:00`
Experiment/version: `EXP-010` / `football_gap_corners_walkforward_fitted_v1`

## Run Summary

- competitions processed: `ENG1, ENG2, ENG3, ENG4, ESP1`
- seasons processed: `2122, 2223, 2324, 2425, 2526`
- statistic: `corners`
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
| ENG1 | 1520 | 2.3018 | 2.1574 | 2.2296 | 0.0337 | -0.0896 |
| ENG2 | 2208 | 2.3271 | 1.9682 | 2.1477 | 0.2030 | -0.0711 |
| ENG3 | 2208 | 2.2366 | 2.0956 | 2.1661 | 0.0638 | -0.1183 |
| ENG4 | 2208 | 2.1890 | 2.0022 | 2.0956 | 0.1175 | -0.1176 |
| ESP1 | 1520 | 2.2789 | 1.8699 | 2.0744 | 0.1314 | -0.0752 |

## Diagnostics by Season

| Season | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2223 | 2416 | 2.2095 | 2.0126 | 2.1111 | 0.2066 | -0.2416 |
| 2324 | 2416 | 2.3316 | 2.0662 | 2.1989 | 0.2205 | 0.0136 |
| 2425 | 2416 | 2.2779 | 1.9772 | 2.1275 | -0.0404 | -0.1383 |
| 2526 | 2416 | 2.2342 | 2.0216 | 2.1279 | 0.0685 | -0.0179 |

## Notes

- Diagnostics are evaluation-only and exclude warm-up-season rows.
- This experiment uses only football-process statistics from the research store.
- No betting simulation, market pricing, or goal conversion is included.