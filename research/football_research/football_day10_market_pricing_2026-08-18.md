# Football Day 10 Market Pricing

Generated at: `2026-08-18T11:34:23+00:00`
Experiment/version: `EXP-013` / `football_day10_market_pricing_v1`

## Interface

- sanitized input policy: `prematch_only_pricing_input_excludes_outcomes_and_market_data_v1`
- goals interface: `persisted_day9_scoreline_matrix_is_canonical_goals_object_v1`
- corners interface: `persisted_day8_corner_distribution_is_canonical_v1`
- settlement pricing: `zero_ev_with_partial_and_push_settlement_v1`

## Engineering Verification Line Catalog

- goals totals: `[1.5, 2.0, 2.25, 2.5, 2.75, 3.0]`
- Asian handicap: `[-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0]`
- corner totals: `[8.5, 9.0, 9.25, 9.5, 9.75, 10.0, 10.5]`

## Market Rows

| Market Family | Rows |
| --- | ---: |
| 1x2 | 28992 |
| asian_handicap | 173952 |
| btts | 19328 |
| corner_total | 135296 |
| goals_total | 115968 |

## Day 11 Note

- Settlement-aware fair prices are preserved directly. Day 11 must define how any derived model_probability or edge field represents push and quarter-line markets.