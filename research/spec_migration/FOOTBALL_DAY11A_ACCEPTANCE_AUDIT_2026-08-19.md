# Football Day 11A Acceptance Audit

Requested artifact filename: `FOOTBALL_DAY11A_ACCEPTANCE_AUDIT_2026-08-19.md`

Actual implementation / verification date: Tuesday, August 18, 2026

## Scope

This audit covers the bounded football Day 11A manager-core build only:

- frozen Day 10 fair-model prices remain the immutable upstream authority
- supplied market input remains provider-independent and explicitly historical/demo
- the build stops at preliminary manager decisions and does not claim live paper readiness

## Acceptance result

Day 11A is accepted as `COMPLETE` for the currently loaded football scope.

## Verified implementation state

- normalized market snapshots are built and versioned by `football_day11_manager_core_v1`
- frozen Day 10 rows are consumed directly; Day 11A does not rebuild fair prices from expected goals
- complete reference markets are booksummed and de-vigged
- detailed `power` and `shin` outputs are persisted, with `shin` used as the configured final method
- best available price is selected from supplied non-reference offers only
- settlement-aware edge is computed as `W * (o - 1) - L`
- minimum acceptable price is computed as `1 + (L + T) / W`
- competition thresholds are applied deterministically
- full-slate decision records are emitted even when outcomes are not positive
- manager-facing Day 11 markdown / CSV / JSONL / latest artifacts are generated
- `DAY11_MANAGER_CORE_READY = YES`
- `LIVE_PAPER_PIPELINE_READY = NO`

## Real run evidence

- real supplied input label: `HISTORICAL / DEMONSTRATION MARKET INPUT`
- current in-repo supplied real market family coverage for the run: `1X2`
- Day 11A decision records: `28992`
- decision counts:
  - `PRELIMINARY_CANDIDATE`: `10299`
  - `NO_BET`: `18693`
  - `REJECTED_RULE`: `0`
  - `MISSING_REQUIRED_MARKET_DATA`: `0`
  - `STALE_MARKET`: `0`
- normalized matched market snapshots: `37240`
- reference markets de-vigged: `9664`

## Required artifacts present

- `research/football_research/football_day11_manager_2026-08-18_normalized_market_snapshots.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_joined_rows.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_devig_outputs.jsonl`
- `research/football_research/football_day11_manager_2026-08-18_manifest.json`
- `research/product_outputs/football/day11/FOOTBALL_DAY11_MANAGER_REPORT.md`
- `research/product_outputs/football/day11/football_day11_decisions.csv`
- `research/product_outputs/football/day11/football_day11_decisions.jsonl`
- `research/product_outputs/football/day11/football_day11_product_manifest.json`
- `research/product_outputs/football/latest/FOOTBALL_DECISIONS.md`
- `research/product_outputs/football/latest/football_decisions.csv`
- `research/product_outputs/football/latest/football_decisions_sample.csv`
- `research/product_outputs/football/latest/FOOTBALL_CURRENT_STATUS.md`

## Boundary checks

- no staking or Kelly layer was added
- no live polling or provider connection was added
- no Day 12 safeguards were implemented here
- no tennis work was started here
- Day 10 remained frozen

## Remaining honest gaps

- live/provider odds ingestion is still unresolved
- current forward-production Day 8 / Day 9 football fit is still unresolved
- Day 12 safeguards / veto foundation is still pending
- full football-spec archive coverage is still pending

## Bottom line

The repo can now derive mathematically correct, reproducible preliminary football manager decisions
from frozen Day 10 fair-model prices and supplied market snapshots without seeing realized outcomes.
That closes Day 11A honestly for the currently loaded scope, but it does not close live paper
readiness.
