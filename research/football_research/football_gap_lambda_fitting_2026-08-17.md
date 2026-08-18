# Football GAP Lambda Fitting

Generated at: `2026-08-17T00:00:00+00:00`
Experiment/version: `EXP-010` / `football_gap_lambda_fitting_v1`

## Fixed Grid

- lambda grid: `0.02, 0.05, 0.10`
- variant count: `45`
- selection policy: `lowest_validation_mae_with_reference_retention_threshold_v1`
- final holdout policy: `latest_ready_season_reserved_until_post_selection_check_v1`

## Selections

| Statistic | Competition | Selected Lambda | Validation Combined MAE | Holdout Season | Holdout Combined MAE | Reason |
| --- | --- | ---: | ---: | --- | ---: | --- |
| corners | ENG1 | 0.05 | 2.2701 | 2526 | 2.1081 | reference lambda retained because the best non-reference candidate improved aggregate validation combined MAE by only 0.008757, below the fixed material threshold 0.01 |
| corners | ENG2 | 0.05 | 2.1504 | 2526 | 2.1393 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| corners | ENG3 | 0.05 | 2.1648 | 2526 | 2.1701 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| corners | ENG4 | 0.05 | 2.0963 | 2526 | 2.0934 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| corners | ESP1 | 0.05 | 2.0592 | 2526 | 2.1200 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots | ENG1 | 0.10 | 3.8450 | 2526 | 3.3725 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots | ENG2 | 0.10 | 3.5298 | 2526 | 3.6168 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots | ENG3 | 0.10 | 3.5237 | 2526 | 3.5101 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots | ENG4 | 0.10 | 3.5279 | 2526 | 3.4955 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots | ESP1 | 0.10 | 3.5036 | 2526 | 3.6915 | selected lowest aggregate validation combined MAE from the fixed lambda grid |
| shots_on_target | ENG1 | 0.05 | 1.8281 | 2526 | 1.6947 | reference lambda retained because the best non-reference candidate improved aggregate validation combined MAE by only 0.006562, below the fixed material threshold 0.01 |
| shots_on_target | ENG2 | 0.05 | 1.6876 | 2526 | 1.7551 | reference lambda retained because the best non-reference candidate improved aggregate validation combined MAE by only 0.000842, below the fixed material threshold 0.01 |
| shots_on_target | ENG3 | 0.05 | 1.7393 | 2526 | 1.8280 | reference lambda retained because the best non-reference candidate improved aggregate validation combined MAE by only 0.009680, below the fixed material threshold 0.01 |
| shots_on_target | ENG4 | 0.05 | 1.7502 | 2526 | 1.7412 | reference lambda retained because the best non-reference candidate improved aggregate validation combined MAE by only 0.005661, below the fixed material threshold 0.01 |
| shots_on_target | ESP1 | 0.05 | 1.7363 | 2526 | 1.6824 | selected lowest aggregate validation combined MAE from the fixed lambda grid |

## Notes

- Validation folds are chronological and stop before the reserved final holdout season.
- Every tested competition/statistic/lambda combination is logged in the JSON artifact.