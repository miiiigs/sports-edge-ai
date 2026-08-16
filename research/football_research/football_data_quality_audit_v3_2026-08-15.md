# Football Data-Quality Audit - Frozen v3

Date: August 15, 2026

## Purpose

This audit checks whether the current `v3` data-quality path is suitable for historical decision-policy research.

It does two separate things:

1. confirms why the original `v3` `data_quality` proxy failed
2. validates the refined `historical_football_point_in_time_dq_v2` contract without using it yet for threshold tuning

## Coverage

- Predicted historical events: `1986`
- Matched market events: `1986`
- Old raw-edge candidates: `636`
- Current reference threshold: `0.82`

## Current v3 Proxy

The original `v3` `data_quality` proxy is still unusable as the historical policy gate.

All predicted events remain compressed into a very narrow band:

- min: `0.7920`
- median: `0.8007`
- max: `0.8016`

All `636` old-policy candidates are below `0.82`, and every one of them fails for the same reason stack:

- `MODEL_NOT_CONVERGED`
- `NONCONVERGENCE_UPDATE_PENALTY`
- `BELOW_DQ_THRESHOLD`

This confirms the original `v3` field is a model-fit proxy, not a full historical decision-quality score.

## Historical DQ v2

The refined `v2` contract now separates:

- `sports_data_quality`
- `model_fit_quality`
- `market_data_quality`
- `decision_data_quality`

This produces much healthier spread.

Decision-quality distribution for all predicted events:

- min: `0.6574`
- p10: `0.7076`
- p25: `0.7240`
- median: `0.7460`
- p75: `0.7707`
- p90: `0.8586`
- max: `0.9272`
- mean: `0.7589`

Decision-quality distribution for old-policy candidates:

- min: `0.6643`
- p10: `0.6996`
- p25: `0.7164`
- median: `0.7401`
- p75: `0.7583`
- p90: `0.8483`
- max: `0.9205`
- mean: `0.7509`

## Below-Threshold Diagnosis

Events below `0.82`:

- all predicted events: `1662 / 1986`
- old-policy candidates: `548 / 636`

Dominant reasons for all below-threshold events:

- `MODEL_NOT_CONVERGED`: `1662`
- `LOW_RECENT_MATCH_COVERAGE`: `1648`
- `HIGH_PREDICTIVE_UNCERTAINTY`: `1431`
- `LOW_PRIOR_SEASON_CONTINUITY`: `622`
- `PROMOTED_OR_NEW_LEAGUE_UNCERTAINTY`: `558`

Dominant reasons for below-threshold old candidates:

- `MODEL_NOT_CONVERGED`: `548`
- `LOW_RECENT_MATCH_COVERAGE`: `542`
- `HIGH_PREDICTIVE_UNCERTAINTY`: `489`
- `LOW_PRIOR_SEASON_CONTINUITY`: `242`
- `PROMOTED_OR_NEW_LEAGUE_UNCERTAINTY`: `221`

## Breakdown

By season phase:

- early season mean decision quality: `0.8233`
- mid season mean decision quality: `0.7488`
- late season mean decision quality: `0.7388`

This is directionally surprising. Early season is currently scoring better than mid and late season, which means the weights are not yet semantically approved.

By continuity flag:

- `continuous_previous_season`: mean `0.7760`
- `previous_season_gap`: mean `0.7151`

By below-threshold continuity split:

- below-threshold `continuous_previous_season`: mean `0.7505`
- below-threshold `previous_season_gap`: mean `0.7151`

By league for below-threshold events:

- `ENG1`: mean `0.7555`
- `ENG2`: mean `0.7281`
- `ENG3`: mean `0.7292`
- `ENG4`: mean `0.7335`
- `ESP1`: mean `0.7564`

## Important Note On Market DQ

Historical `market_data_quality` is structurally capped in the current audit because:

- `timestamp_quality_score` is only fixed partial credit when a quote exists
- `entry_close_availability_score` is always `0.0` without trusted historical timing data

That is honest, but it must not be misread as weak sports data or weak model quality.

## Interpretation

The refined `v2` contract is much better than the original `v3` proxy.

But it is not yet conceptually approved.

Why:

- the distribution now has useful spread
- the dominant low-score reasons are meaningful
- but early-season behavior still looks counterintuitive
- and the weakest-history split is still not very informative in this historical sample

## Current Decision

Accept `historical_football_point_in_time_dq_v2` as the current research contract.

Do not yet use it for threshold optimization.

Do not lower the decision threshold just to create more candidates.

The next step is to keep validating the semantics of `v2`, especially:

- why early season scores so highly
- how much `model_fit_quality` should influence final decision quality
- whether recent-coverage and continuity weights are conceptually right
