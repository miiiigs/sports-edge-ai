# FOOTBALL MODEL PERFORMANCE REPORT

- generated at: `2026-08-20T04:18:32+00:00`
- benchmark version: `football_model_performance_benchmark_v1`
- MODEL_VALIDATION: `PROMISING`
- PROFITABILITY_PROVEN: `NO`
- CLV_PROVEN: `NO`
- LIVE_EDGE_PROVEN: `NO`

## Manager Summary

- The frozen football model is technically `promising` over `9664` historical fixtures.
- 1X2 top-probability accuracy: `47.05%`.
- 1X2 multiclass Brier: `0.6251`.
- 1X2 multiclass log loss: `1.0395`.
- Goals MAE: `1.276`.
- Corners MAE: `2.696`.
- Calibration: `average expected calibration error 0.0157`.
- Strongest market in the current measurable slice: `GOALS_TOTAL`.
- Weakest market in the current measurable slice: `1X2`.
- Historical profitability: `Only closing 1X2 snapshots exist historically, so entry-time ROI and CLV are not proven.`.
- CLV availability: `UNAVAILABLE_HISTORICALLY`.

## Chronology

- rows checked: `9664`
- `as_of > kickoff`: `0`
- `as_of == kickoff`: `9664`
- chronology status: `PASS`
- source-time check: `UNAVAILABLE_IN_FROZEN_ARCHIVE`

## Historical Price Context

- Historical market quotes in the loaded repo are `1X2` closing snapshots only.
- They are valid for market-comparison context and historical manager demonstration rows.
- They are not valid proof of entry-time ROI or CLV.
- preliminary candidate count from Day 11A demo lane: `10299`
- average closing snapshot price across those demo candidates: `4.736`
- closing-snapshot break-even rate: `21.12%`

## Market Benchmark

| Market | Line | Selection | Sample | Hit Rate | Brier | Log Loss | Status |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 1X2 | ALL | ALL | 9664 | 47.05% | 0.6251 | 1.0395 | READY |
| BTTS | ALL | YES | 9664 | 52.57% | 0.2480 | 0.6892 | READY |
| GOALS_TOTAL | 1.5 | OVER | 9664 | 74.21% | 0.1892 | 0.5651 | READY |
| CORNER_TOTAL | 8.5 | OVER | 9664 | 65.34% | 0.2256 | 0.6437 | READY |
| ASIAN_HANDICAP | ALL | ALL | 0 | N/A | N/A | N/A | SETTLEMENT_READY |

## Competition Breakdown

| Competition | Fixtures | 1X2 Accuracy | 1X2 Brier | 1X2 Log Loss | Goals MAE | Corners MAE | Candidate Count | Status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| ENG1 | 1520 | 52.04% | 0.5948 | 0.9975 | 1.330 | 2.733 | 1910 | PRICE_CONTEXT_CLOSING_ONLY |
| ENG2 | 2208 | 44.70% | 0.6381 | 1.0576 | 1.239 | 2.736 | 2243 | PRICE_CONTEXT_CLOSING_ONLY |
| ENG3 | 2208 | 46.88% | 0.6311 | 1.0473 | 1.261 | 2.764 | 2218 | PRICE_CONTEXT_CLOSING_ONLY |
| ENG4 | 2208 | 44.29% | 0.6441 | 1.0656 | 1.297 | 2.628 | 2099 | PRICE_CONTEXT_CLOSING_ONLY |
| ESP1 | 1520 | 49.74% | 0.6006 | 1.0061 | 1.265 | 2.601 | 1829 | PRICE_CONTEXT_CLOSING_ONLY |

## Forecast Error

- expected total goals MAE: `1.276`
- expected total corners MAE: `2.696`
- shots total MAE: `4.500`
- shots on target total MAE: `2.382`
