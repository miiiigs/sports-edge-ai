# Football Day 8 Goal Conversion

Generated at: `2026-08-17T17:34:14+00:00`
Experiment/version: `EXP-011` / `football_day8_goal_conversion_v1`

## Goal Conversion

- conversion model version: `football_day8_goal_conversion_v1`
- conversion form: `league_and_venue_nonnegative_linear_shots_plus_sot_v1`
- conversion variant count: `40`

## Overall Goal Diagnostics

- mean predicted home goals: `1.5063`
- mean observed home goals: `1.4606`
- mean predicted away goals: `1.1409`
- mean observed away goals: `1.1823`
- mean predicted total goals: `2.6472`
- mean observed total goals: `2.6429`
- total-goal MAE: `1.2756`
- total-goal bias: `0.0043`

## Corner Distribution Selection

| Competition | Selected Distribution | Cum. Poisson LL | Cum. NB LL | Folds |
| --- | --- | ---: | ---: | ---: |
| ENG1 | negative_binomial | -4008.1738 | -4003.4397 | 4 |
| ENG2 | negative_binomial | -5840.5414 | -5829.9976 | 4 |
| ENG3 | negative_binomial | -5867.5592 | -5851.4868 | 4 |
| ENG4 | negative_binomial | -5758.1474 | -5752.0922 | 4 |
| ESP1 | negative_binomial | -3946.2792 | -3940.3240 | 4 |