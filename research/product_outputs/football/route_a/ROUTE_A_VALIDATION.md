# Route A Validation

## Previous Validation

Status: `SUPERSEDED / INVALIDATED`
- prior Route A validation used Day 7 hyperparameters selected with future seasons
- prior Route A calibration choice compared candidates on outer test outcomes

## Corrected Validation

Generated: `2026-08-27T18:38:50+00:00`
Artifact id: `route_a_model_81df7cf6e60f03fb52a7a8a3ec9c66db8bf0ddae6764c69f1197ae7b9c02977e`
Artifact schema version: `football_route_a_direct_classifier_artifact_v2`
Model bundle version: `football_route_a_direct_classifier_bundle_v2`
Agent id: `ROUTE_A_DIRECT_CLASSIFIER`
Overall runtime state: `REJECTED`

## Dataset

- source: `research/football_research/store/football_research.sqlite`
- rows: `9664`
- match date range: `2022-07-29` to `2026-05-24`
- seasons: `2223, 2324, 2425, 2526`
- competitions: `ENG1, ENG2, ENG3, ENG4, ESP1`

## Contract

- model: `LOGISTIC_REGRESSION`
- features: `predicted_home_shots, predicted_away_shots, predicted_home_corners, predicted_away_corners, predicted_total_shots, predicted_shot_difference, predicted_total_corners, predicted_corner_difference, competition_id`
- targets: `BTTS, OVER_2_5`
- markets: `BTTS`, `goals_total 2.5`

## Corrected GAP Provenance

- baseline policy: `warmup_first_ready_season_league_venue_average_v1`
- lambda policy: `lowest_validation_mae_with_reference_retention_threshold_v1`
- as_of policy: `historical_prediction_as_of_fixture_kickoff_v1`
- hyperparameter cutoff violations: `0`
- training cutoff violations: `0`
- prediction as_of violations: `0`

## Walk-Forward Windows

- train `2223`; calibration `2324`; test `2425`
- train `2223, 2324`; calibration `2425`; test `2526`

## Selection Guard

Calibration selection uses only dedicated calibration seasons inside each fold, so outer test outcomes cannot influence the chosen deployment method.

## BTTS

- baseline: `LEAGUE_BASE_RATE`
- secondary baseline: `GLOBAL_BASE_RATE`
- selected candidate calibration for the pre-holdout artifact: `isotonic`
- market verdict: `REJECTED`

| Test Period | N | Baseline LL | Route A LL | Delta | Baseline Brier | Route A Brier | Delta | Calibration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2425 | 2416 | 0.692622 | 0.851256 | -0.158634 | 0.249739 | 0.253568 | -0.003829 | isotonic |
| 2526 | 2416 | 0.691036 | 0.696234 | -0.005198 | 0.248946 | 0.247713 | 0.001233 | isotonic |

Aggregate:
- baseline log loss: `0.691829`
- Route A log loss: `0.773745`
- baseline Brier: `0.249342`
- Route A Brier: `0.250640`
- calibration intercept: `0.137901`
- calibration slope: `0.032913`
- binned reliability slope: `0.248912`
- ECE: `0.025334`
- bootstrap mean delta log loss: `-0.081916`
- bootstrap log loss 95% CI: `-0.124527` to `-0.047547`

Competition stability:
- `ENG1`: delta log loss `-0.084772`, delta Brier `-0.006027`, N `760`
- `ENG2`: delta log loss `-0.032795`, delta Brier `0.000219`, N `1104`
- `ENG3`: delta log loss `-0.254212`, delta Brier `-0.003316`, N `1104`
- `ENG4`: delta log loss `-0.016836`, delta Brier `0.000028`, N `1104`
- `ESP1`: delta log loss `0.005329`, delta Brier `0.002234`, N `760`

Route A / Route B diversity:
- probability correlation: `n/a`
- direction disagreement: `n/a`
- spread <= 0.06: `n/a`
- historical consensus pass rate: `n/a`

## Over 2.5

- baseline: `LEAGUE_BASE_RATE`
- secondary baseline: `GLOBAL_BASE_RATE`
- selected candidate calibration for the pre-holdout artifact: `isotonic`
- market verdict: `REJECTED`

| Test Period | N | Baseline LL | Route A LL | Delta | Baseline Brier | Route A Brier | Delta | Calibration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2425 | 2416 | 0.690884 | 0.986807 | -0.295922 | 0.248869 | 0.256916 | -0.008047 | isotonic |
| 2526 | 2416 | 0.693502 | 0.705475 | -0.011973 | 0.250175 | 0.248645 | 0.001530 | isotonic |

Aggregate:
- baseline log loss: `0.692193`
- Route A log loss: `0.846141`
- baseline Brier: `0.249522`
- Route A Brier: `0.252780`
- calibration intercept: `-0.002671`
- calibration slope: `0.029932`
- binned reliability slope: `0.399778`
- ECE: `0.033813`
- bootstrap mean delta log loss: `-0.153948`
- bootstrap log loss 95% CI: `-0.203783` to `-0.106644`

Competition stability:
- `ENG1`: delta log loss `-0.009410`, delta Brier `-0.003718`, N `760`
- `ENG2`: delta log loss `-0.073134`, delta Brier `-0.003467`, N `1104`
- `ENG3`: delta log loss `-0.342700`, delta Brier `-0.006015`, N `1104`
- `ENG4`: delta log loss `-0.118161`, delta Brier `-0.002058`, N `1104`
- `ESP1`: delta log loss `-0.193676`, delta Brier `-0.000238`, N `760`

Route A / Route B diversity:
- probability correlation: `n/a`
- direction disagreement: `n/a`
- spread <= 0.06: `n/a`
- historical consensus pass rate: `n/a`

## Manifest

- review status: `PENDING_INDEPENDENT_REVIEW`
- validation status: `FAILED`
- validated markets: `none`

## Runtime Gate

- Route A remains disabled until the exact artifact checksum and independent review are approved.
- Pending review and rejected markets both fail closed.
