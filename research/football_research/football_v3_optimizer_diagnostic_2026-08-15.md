# Football v3 Optimizer Diagnostic

Date: August 15, 2026

## Purpose

This diagnostic checks how the frozen `poisson_1x2_v3` optimizer behaves numerically without changing the model family or selecting anything based on ROI.

The question is:

- are the fits still moving materially after iteration `250`?
- are they oscillating?
- are they slowly converging?
- or are they mostly stable while still formally above tolerance?

## Coverage

- Predicted events audited: `1986`
- Competitions: `ENG1`, `ENG2`, `ENG3`, `ENG4`, `ESP1`
- Strength regularization: `0.03`
- Checkpoints: `1`, `5`, `10`, `25`, `50`, `100`, `150`, `250`, `500`, `1000`
- Formal tolerance: `1e-5`

## Main Result

All `1986` audited fits are still formally `non_converged`.

However, the longer-run diagnostic changes the interpretation compared with the earlier 250-iteration-only snapshot:

- by `1000` iterations, every fit is within `5x` tolerance
- all fits are within `10x` tolerance
- all fits are within `25x` tolerance

Final update distribution at `1000` iterations:

- min: `2.47e-05`
- median: `3.45e-05`
- max: `3.87e-05`

Tolerance-ratio distribution at `1000` iterations:

- min: `2.47x`
- median: `3.45x`
- max: `3.87x`

This is no longer the same picture as the old `18x+` result from the shorter run.

## Trajectory Classification

Across all `1986` events:

- `slowly_converging`: `1162`
- `stable_nonzero_update`: `824`
- `oscillating`: `0`

This is important.

The current diagnostic does **not** support “wild oscillation” as the dominant problem.

The two dominant states are:

- still converging slowly
- or already fairly stable in probabilities while the formal update criterion remains above tolerance

## 250 vs 500

From iteration `250` to `500`:

- mean objective improvement: `0.00160`
- median max probability change: `0.00408`
- mean max probability change: `0.00546`
- material probability change rate (`>= 0.005`): `42.15%`

Interpretation:

Iteration `250` is still too early for a large share of events.

## 500 vs 1000

From iteration `500` to `1000`:

- mean objective improvement: `0.00058`
- median max probability change: `0.00197`
- mean max probability change: `0.00336`
- material probability change rate (`>= 0.005`): `23.67%`

Interpretation:

The optimizer is still moving, but materially less than in the `250 -> 500` interval.

## 250 vs 1000

From iteration `250` to `1000`:

- mean objective improvement: `0.00218`
- median max probability change: `0.00609`
- mean max probability change: `0.00880`
- material probability change rate (`>= 0.005`): `58.51%`

Interpretation:

For more than half the events, probabilities still change materially between iteration `250` and `1000`.

That means the current `250`-iteration fit should not be treated as numerically settled.

## Conclusion

This result is more nuanced than “v3 is broken.”

What it supports:

- `v3` is not merely missing convergence by harmless numerical dust at iteration `250`
- a large share of events are still moving materially after `250`
- by `1000`, updates are much closer to tolerance
- the dominant modes are `slowly_converging` and `stable_nonzero_update`, not oscillation

What it does **not** support yet:

- automatically trusting `converged=False` as a full model-quality failure
- automatically assuming the current tolerance is the right operational criterion

## Current Decision

Keep `v3` predictive specification frozen.

Do not mix this optimizer issue into policy-threshold tuning.

The next optimizer-specific step should be:

1. compare forecasting metrics and probability deltas under `250`, `500`, and `1000` iteration caps
2. determine whether the extra iterations improve forecasts materially or mostly just satisfy the numerical criterion
3. if probabilities become nearly unchanged while updates remain above tolerance, revisit the convergence criterion or optimization method
4. if probabilities keep moving materially, continue treating `model_fit_quality` conservatively in the decision layer
