# Football GAP Shots Walk-Forward

Generated at: `2026-08-15T22:57:36+00:00`
Experiment/version: `EXP-007` / `football_gap_shots_walkforward_v1`

## Run Summary

- competitions processed: `ENG1, ENG2, ENG3, ENG4, ESP1`
- seasons processed: `2122, 2223, 2324, 2425, 2526`
- statistic: `shots`
- fixed learning rate: `0.05` (`REFERENCE / UNOPTIMIZED`)
- baseline policy: `warmup_first_ready_season_league_venue_average_v1`
- matches processed: `12080`
- predictions produced: `12080`
- evaluation predictions: `9664`
- events skipped: `0`

## Policies

- warm-up policy: The earliest readiness-approved season per competition is treated as a warm-up season. Predictions are exported for reproducibility, but evaluation diagnostics exclude warm-up rows.
- season-transition policy: Carry GAP state forward across seasons within the same competition. Do not reset at season boundaries. Teams newly appearing in a competition start from the competition baseline with zero current-competition matches observed.
- duplicate-event policy: Reject duplicate observations inside GapRatingsEngine by (competition_id, statistic, event_id) so the same event cannot move ratings twice.

## Diagnostics by Competition

| Competition | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ENG1 | 1520 | 3.9159 | 3.6365 | 3.7762 | 0.2531 | -0.1792 |
| ENG2 | 2208 | 3.7461 | 3.3898 | 3.5679 | 0.5478 | -0.1016 |
| ENG3 | 2208 | 3.6667 | 3.4570 | 3.5619 | 0.1327 | -0.3697 |
| ENG4 | 2208 | 3.6866 | 3.4057 | 3.5461 | 0.2214 | -0.2562 |
| ESP1 | 1520 | 3.7464 | 3.4025 | 3.5745 | 0.4448 | -0.0722 |

## Diagnostics by Season

| Season | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2223 | 2416 | 3.6057 | 3.4366 | 3.5212 | 0.3765 | -0.5252 |
| 2324 | 2416 | 3.9024 | 3.5125 | 3.7074 | 0.7325 | 0.1495 |
| 2425 | 2416 | 3.7809 | 3.4452 | 3.6131 | -0.1205 | -0.4409 |
| 2526 | 2416 | 3.6753 | 3.4041 | 3.5397 | 0.2747 | -0.0064 |

## Notes

- Diagnostics are evaluation-only and exclude warm-up-season rows.
- This experiment uses only football-process statistics from the research store.
- No betting simulation, market pricing, lambda optimization, or goal conversion is included.