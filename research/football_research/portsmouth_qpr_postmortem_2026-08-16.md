# Portsmouth vs QPR Postmortem

Date reviewed: 2026-08-16

## Locked Live Candidate

- Match: Portsmouth vs QPR
- Market: 1X2
- Selection: home
- Entry price: 2.40
- Fair odds: 2.21
- Model probability: 45.2%
- Raw break-even probability: 41.7%
- Raw estimated edge: +3.55%
- Model: frozen `poisson_1x2_v3`
- Policy version at decision time: `football_watchlist_gate_v1`
- Final score: Portsmouth 1-3 QPR
- Paper result: LOSS, -1.00u
- Closing price: unavailable at settlement time
- CLV: unavailable until a trusted final pre-kickoff price is captured

## What The Model Knew

The model used blind historical football data only. It produced a home probability of about 45.2%, with expected goals around 1.48 for Portsmouth and 1.13 for QPR. The market price was captured after the prediction lock, so the sports/market firewall was preserved.

## Why The Old Gate Accepted It

The old live gate compared the model point estimate against raw market break-even:

```text
45.2% model probability - 41.7% raw break-even = +3.55% edge
```

Because this cleared the old 3% threshold and the snapshot was pre-kickoff, it became a paper/shadow candidate.

## What Was Missing

- No trusted final pre-kickoff closing price was captured.
- The candidate gate did not yet require de-vigged market comparison.
- The candidate gate did not require conservative lower-bound probability value.
- The candidate gate did not reject non-converged v3 fits.
- The candidate gate did not use league/selection reliability as a risk modifier.

## Historical Slice Check

Frozen v3 was audited for the Portsmouth-like historical slice:

```text
competition: ENG2
selection: home
model probability: 40-50%
edge: 3-5%
fair odds: <= 2.80
```

Result:

```text
sample size: 14
win rate: 42.9%
average odds: 2.57
ROI per bet: +8.0%
calibration gap: +0.12 percentage points
average model-market discrepancy: +6.69 percentage points
CLV: unavailable
```

This is too small to create a hard rule. It does not prove the Portsmouth-style setup is bad. The better lesson is that the live decision layer needed stricter robustness checks before allowing any candidate through.

## Future Gate Impact

Under the stricter decision layer added after this postmortem, this candidate would be blocked by the robustness layer because the v3 fit was not fully converged and the data quality was reduced. The likely future reason code would be:

```text
PASS_LOW_DATA_QUALITY
```

or, depending on exact ordering:

```text
PASS_MODEL_UNSTABLE
```

The model itself should not be changed because of this single loss. This is a decision-layer postmortem, not training data.
