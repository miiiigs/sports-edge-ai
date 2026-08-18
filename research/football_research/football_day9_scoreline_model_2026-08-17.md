# Football Day 9 Scoreline Model

Generated at: `2026-08-17T18:37:41+00:00`
Experiment/version: `EXP-012` / `football_day9_scoreline_model_v1`

## Interface

- policy: `day8_expected_goals_are_day9_intensities_v1`
- gamma handling: `gamma_omitted_to_avoid_double_counting_day8_venue_effects_v1`

## Overall Diagnostics

- plain Poisson log likelihood: `-27914.3931`
- Dixon-Coles log likelihood: `-27917.2423`
- improvement: `-2.8491`
- average draw probability difference: `0.001214`
- average tail mass beyond 10: `0.000002`
- max normalization error: `0.000000000000`

## Final Rho By Competition

| Competition | Final Rho | Selected Xi | Latest Season |
| --- | ---: | ---: | --- |
| ENG1 | +0.00 | 0.0000 | 2526 |
| ENG2 | +0.00 | 0.0000 | 2526 |
| ENG3 | +0.00 | 0.0000 | 2526 |
| ENG4 | -0.05 | 0.0000 | 2526 |
| ESP1 | +0.00 | 0.0000 | 2526 |

## Time Decay Summary

| Competition | Selected Xi | Cumulative OOS LL |
| --- | ---: | --- |
| ENG1 | 0.0000 | {'0.0000': -3414.9721247855177, '0.0010': -3414.9721247855177} |
| ENG2 | 0.0000 | {'0.0000': -4709.696288654277, '0.0010': -4709.696288654277} |
| ENG3 | 0.0000 | {'0.0000': -4761.077159485858, '0.0010': -4761.077159485858} |
| ENG4 | 0.0000 | {'0.0000': -4901.22732885671, '0.0010': -4901.22732885671} |
| ESP1 | 0.0000 | {'0.0000': -3235.9768540415944, '0.0010': -3235.9768540415944} |