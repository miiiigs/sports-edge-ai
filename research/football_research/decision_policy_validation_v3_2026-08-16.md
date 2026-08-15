# Football Decision Policy Validation - Frozen v3

Date: 2026-08-16

## Purpose

This validation compares the old football decision policy against the new robust decision gate using frozen `poisson_1x2_v3`.

This is not a model retune. The goal is to answer:

- Did the old raw `edge >= 3%` rule produce good historical candidate quality?
- Does the new stricter gate reject weak candidates before live/paper selection?
- Which gate is responsible for rejections?

## Setup

- Model: `poisson_1x2_v3`
- Model status: promoted and frozen
- Strength regularization: `0.03`
- Target season: `2526`
- History seasons: `2122`, `2223`, `2324`, `2425`
- Competitions: `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
- Bookmaker: `Bet365`
- Old gate: raw model edge `>= 3%`
- New robust gate: raw edge plus data quality, uncertainty, model stability, fair-odds cap, and reliability checks

## Coverage

- Target events: `2416`
- Events with blind model prediction: `1986`
- Events with matched market prices: `1986`
- Selection evaluations: `5958`

## Main Result

The old policy produced many candidates, but the historical quality was weak.

Old raw-edge policy:

- Candidates: `636`
- ROI per bet: `-15.49%`
- Win rate: `30.50%`
- Average odds: `3.05`
- Average model probability: `44.03%`
- Realized rate: `30.50%`
- Calibration gap: model overstated selected bets by about `13.53 percentage points`
- Max drawdown: `-106.21u`

New robust policy:

- Candidates: `0`
- All old candidates were rejected by the new gate
- Rejection reason for old candidates: `PASS_LOW_DATA_QUALITY`

## Interpretation

The old policy was too permissive. It allowed many candidates because the point-estimate model edge looked positive, but the selected bets were historically overconfident and negative ROI overall.

The new gate is much safer, but in this exact historical validation it is also too restrictive to produce any candidates. That is not a failure of the model. It means the current robust gate is functioning as a hard skeptic and needs to be evaluated carefully before being used as the final production policy.

The correct next step is not to loosen thresholds randomly. The next step is a controlled policy-threshold study on historical data only, while keeping frozen `v3` unchanged.

## League Notes

Old-policy ROI by league:

- `ENG1`: `-30.43%`
- `ENG2`: `-18.41%`
- `ENG3`: `-7.67%`
- `ENG4`: `-19.99%`
- `ESP1`: `+2.86%`

`ESP1` was the only slightly positive old-policy league in this run, but it still showed model overstatement and should not be treated as proven edge by itself.

## Selection Notes

Old-policy ROI by selection:

- Home: `-17.62%`
- Away: `-6.69%`

No draw candidates survived the old gate under this configuration.

## Decision

Keep `v3` frozen.

Do not return to the old raw-edge gate.

Treat the current robust gate as safe but too restrictive until a controlled threshold validation finds a better balance between candidate quality and candidate volume.

Next recommended experiment:

- Run a validation-only threshold grid over decision-layer parameters
- Compare candidate count, ROI, calibration, average odds, drawdown, league results, and selection results
- Do not select thresholds from one live loss
- Do not modify the probability model during this decision-policy study
