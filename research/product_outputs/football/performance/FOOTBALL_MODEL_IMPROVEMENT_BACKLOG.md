# FOOTBALL MODEL IMPROVEMENT BACKLOG

1. Add a genuine current odds provider and append-only entry-time market capture so CLV and real paper ROI can be measured.
2. Expand historical market coverage beyond closing 1X2 snapshots so BTTS, totals, Asian handicap, and corners can be benchmarked against true market prices.
3. Review the weakest currently measurable market slice: `1X2`.
4. Preserve the no-leakage benchmark discipline. Current `as_of > kickoff` count is `0` and must remain zero.
5. Tighten candidate-selection evidence only after better market capture exists. Current historical demo candidate count is `10299` but price timing is still closing-only.
6. If the validation state remains `PROMISING`, prioritize calibration and market data quality before any model retuning.
