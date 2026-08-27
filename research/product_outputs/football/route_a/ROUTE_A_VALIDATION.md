# Route A Validation

Generated: `2026-08-27T12:52:56+00:00`
Artifact id: `route_a_artifact_6ab67ff66bd3629c85bfc6d529023dfbfd2a1065f3fef33fae68c12d2391c85e`
Artifact version: `football_route_a_direct_classifier_bundle_v1`
Agent id: `ROUTE_A_DIRECT_CLASSIFIER`
Overall status: `ACTIVE`

## Dataset

- source: `research/football_research/store/football_research.sqlite` via Day 7 selected GAP replay joins
- rows: `12080`
- match date range: `2021-08-06` to `2026-05-24`
- seasons: `2122, 2223, 2324, 2425, 2526`
- competitions: `ENG1, ENG2, ENG3, ENG4, ESP1`

## Contract

- model: `LOGISTIC_REGRESSION`
- features: `predicted_home_shots, predicted_away_shots, predicted_home_corners, predicted_away_corners, predicted_total_shots, predicted_shot_difference, predicted_total_corners, predicted_corner_difference, competition_id`
- targets: `BTTS, OVER_2_5`
- markets: `BTTS`, `goals_total 2.5`

## Point-In-Time Controls

- features originate from chronological Day 7 GAP predictions only
- targets are derived from realized match goals after feature generation
- walk-forward folds are season-ordered and calibration uses only prior seasons
- no closing prices, final scores, future fixtures, or future ratings enter the feature set

## Walk-Forward Windows

- train `2122`; calibration `none`; test `2223`
- train `2122, 2223`; calibration `2223`; test `2324`
- train `2122, 2223, 2324`; calibration `2324`; test `2425`
- train `2122, 2223, 2324, 2425`; calibration `2425`; test `2526`

## BTTS

- baseline: `LEAGUE_BASE_RATE`
- selected calibration: `none`
- accepted: `YES`

### Naive baseline

- `LEAGUE_BASE_RATE` sample count: `9664`
- `LEAGUE_BASE_RATE` log loss: `0.692930`
- `LEAGUE_BASE_RATE` Brier: `0.249892`
- `LEAGUE_BASE_RATE` calibration slope: `nan`
- `LEAGUE_BASE_RATE` expected calibration error: `0.020738`

### Uncalibrated logistic

- `uncalibrated` sample count: `9664`
- `uncalibrated` log loss: `0.691299`
- `uncalibrated` Brier: `0.249067`
- `uncalibrated` calibration slope: `0.346699`
- `uncalibrated` expected calibration error: `0.022217`

### Selected calibrated logistic

- `none` sample count: `9664`
- `none` log loss: `0.691299`
- `none` Brier: `0.249067`
- `none` calibration slope: `0.346699`
- `none` expected calibration error: `0.022217`

### Other calibration candidates

- `isotonic` sample count: `7248`
- `isotonic` log loss: `0.710781`
- `isotonic` Brier: `0.249860`
- `isotonic` calibration slope: `-0.270577`
- `isotonic` expected calibration error: `0.028033`
- `platt` sample count: `7248`
- `platt` log loss: `0.691825`
- `platt` Brier: `0.249315`
- `platt` calibration slope: `-0.315461`
- `platt` expected calibration error: `0.027613`

## Over 2.5

- baseline: `LEAGUE_BASE_RATE`
- selected calibration: `none`
- accepted: `YES`

### Naive baseline

- `LEAGUE_BASE_RATE` sample count: `9664`
- `LEAGUE_BASE_RATE` log loss: `0.692669`
- `LEAGUE_BASE_RATE` Brier: `0.249756`
- `LEAGUE_BASE_RATE` calibration slope: `nan`
- `LEAGUE_BASE_RATE` expected calibration error: `0.015625`

### Uncalibrated logistic

- `uncalibrated` sample count: `9664`
- `uncalibrated` log loss: `0.687043`
- `uncalibrated` Brier: `0.246980`
- `uncalibrated` calibration slope: `1.069192`
- `uncalibrated` expected calibration error: `0.016743`

### Selected calibrated logistic

- `none` sample count: `9664`
- `none` log loss: `0.687043`
- `none` Brier: `0.246980`
- `none` calibration slope: `1.069192`
- `none` expected calibration error: `0.016743`

### Other calibration candidates

- `isotonic` sample count: `7248`
- `isotonic` log loss: `0.759937`
- `isotonic` Brier: `0.249585`
- `isotonic` calibration slope: `0.286337`
- `isotonic` expected calibration error: `0.026557`
- `platt` sample count: `7248`
- `platt` log loss: `0.688665`
- `platt` Brier: `0.247770`
- `platt` calibration slope: `0.789518`
- `platt` expected calibration error: `0.021203`

## Decision

- Route A overall activation: `ACTIVE`
- accepted markets: `btts, goals_total_2.5`
- calibration selection stayed evidence-based and was allowed to remain `none` where held-out calibration did not improve the market.
- known limitation: the accepted baseline stays intentionally narrow to original GAP shots/corners plus league identity rather than broader engineered features.
