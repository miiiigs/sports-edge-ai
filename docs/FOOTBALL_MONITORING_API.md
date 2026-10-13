# Football monitoring API (`football_monitoring_v1`)

Read-only contract for the unified Sports Edge dashboard, which is hosted from
the Tennis repository. Football owns the runtime, the ledgers and this endpoint;
Tennis consumes it server-side.

## Access

| | |
|---|---|
| Route | `GET` / `HEAD` `/api/monitoring/football` |
| Auth | Owner session cookie, **or** `Authorization: Bearer $SPORTS_EDGE_CLIENT_API_TOKEN` (server-to-server only) |
| Token scope | GET/HEAD on exactly 4 paths: `/api/candidates`, `/api/performance`, `/api/system/status`, `/api/monitoring/football`. All other routes, including every POST, return 401. |
| Side effects | None. Every DB is opened `mode=ro`, and no provider request is made. |

**Never expose the token to a browser.** The Tennis host must call Football from
its server and render or forward only the JSON body. The token must not be set in
any client-side env var (`NEXT_PUBLIC_*`, `VITE_*`, …), cookie or HTML.

## Document

```jsonc
{
  "schema_version": "football_monitoring_v1",
  "sport": "football",
  "generated_at": "2026-10-13T12:00:00+00:00",
  "read_only": true,
  "deployed_commit": "6c67200…",            // RAILWAY_GIT_COMMIT_SHA or null
  "sources": {"dashboard_db": true, "paper_db": true, "model_pick_db": true},
  "operational": { … },
  "model_performance": { … },
  "qualified_betting": { … },
  "high_confidence_shadow": { … }
}
```

### `operational` — is the system working?

| Field | Meaning |
|---|---|
| `schedulers.<name>` | For `market_collection`, `result_sync`, `model_pick_settlement`, `model_pick_issuance`, `prediction_refresh`, `value_evaluation` and `high_confidence_shadow`: `enabled`, `last_cycle_at`, `last_success_at`, `last_error_present` (boolean; error text is never exposed), `cycles_completed`, `hours_since_success`, `stale` (enabled and no success in 24h) |
| `latest_forward_model_pick_issued_at` | Latest `created_at` of a forward LIVE_ISSUED pick |
| `latest_prediction_generated_at` | Latest prediction version timestamp |
| `prediction_coverage` | Next 72h: `upcoming_fixtures`, `with_prediction`, `latest_ready`, `ready_without_model_pick`, `latest_not_ready_by_status`, `ready_rate` |
| `settlement_backlog` | LIVE_ISSUED picks still open 3h after kickoff: count, oldest kickoff, max lag |
| `last_issuance_cycle` | `written`, `canonical_duplicates`, `conflicts` from the last issuance cycle. `written: 0` with `ready_without_model_pick > 0` is an abnormal zero-pick cycle. |
| `providers` | `fixture_provider_state`, `provider_budget_quota_state`, `market_sync_state` |
| `odds_freshness[]` | Per provider × market: `latest_captured_at`, `age_minutes`, `snapshots_24h`, `stale` (> 180 min) |
| `alerts[]` | Codes: `SCHEDULER_STALE`, `SCHEDULER_LAST_CYCLE_ERROR`, `READY_FIXTURES_WITHOUT_PICK`, `NO_READY_PREDICTIONS_FOR_UPCOMING`, `SETTLEMENT_LAG`, `ALL_ODDS_STALE`, `FIXTURE_PROVIDER_NOT_CONNECTED`, `PROVIDER_QUOTA_NOT_HEALTHY` |

### `model_performance` — which predictions were right or wrong?

The headline cohort is **`LIVE_ISSUED_FORWARD`**: `record_origin = LIVE_ISSUED`
**and** `created_at < kickoff_utc`.

Every cohort object has the same shape:

```jsonc
{
  "issued": 0, "settled": 0, "open": 0,
  "states": {"FULL_WIN": 0, "HALF_WIN": 0, "PUSH": 0, "HALF_LOSS": 0, "FULL_LOSS": 0},
  "decisive_stake_units": 0.0,
  "win_share_stake_weighted": null,   // (FW + ½HW) / (FW + ½HW + FL + ½HL); pushes excluded
  "ci95": [null, null],               // Wilson, n = decisive stake units
  "mean_q": null,                     // mean push-excluded model win probability (1 / fair)
  "calibration_gap": null,            // win_share − mean_q
  "insufficient_sample": true,
  "min_sample": 30
}
```

| Field | Meaning |
|---|---|
| `forward` | Headline cohort (`min_sample` 100) |
| `forward_by_market`, `forward_by_competition`, `forward_by_confidence_band`, `forward_by_model_version` | Breakdowns. The bands are `q<0.50`, `0.50-0.55`, `0.55-0.60`, `0.60-0.70` and `q>=0.70`. |
| `high_confidence_misses[]` | Up to 25 forward FULL_LOSS picks with q ≥ 0.60, highest q first |
| `excluded_from_headline` | `live_issued_not_forward` and `current_system_replay` counts |
| `historical_replay` | The replay cohort, kept separate and never pooled. `null` when there are no replay rows. |
| `market_benchmark` | `forward_available: false` until closing prices are linked to Model Picks. The holdout model-vs-market comparison is in `docs/FOOTBALL_MODEL_ACCURACY_AUDIT.md`. |

### `qualified_betting` — Value Bets only

Only frozen value-pipeline rows (`value_pipeline_version` set) count. Model Picks
and legacy paper positions are **not** qualified bets. Legacy AH rows that fail
the #106 evidence rule are quarantined.

`value_bets`, `quarantined_value_bets`, `legacy_paper_positions_excluded`,
`settled`, `open`, `units_net`, `roi`, `average_clv`, `clv_state`,
`max_drawdown`, `by_bookmaker`, `max_suggested_stake_fraction`,
`stake_cap_breaches`, `open_exposure_stake_fraction`, `insufficient_sample`
(min 100), `value_bet_issuance_enabled`, `staking_policy`
(`QUARTER_KELLY_CAP_2_V1`: 0.25 Kelly, 2% cap), and
`profitability_claim: "NONE"`.

### `high_confidence_shadow`

`{"available": false, "enabled": false, "policy_id": …}` until the lane is enabled.
After that it carries the lane's own `performance()`: `issued`, `settled`,
`win_share_stake_weighted`, `ci95`, `mean_q_settled`, `calibration_gap`,
`coverage`, `abstentions`, `offered_price_observed`, `mean_offered_over_fair`,
`insufficient_sample` (min 300) and `value_claim: "NONE…"`.

## Display rules for the unified dashboard

1. A `null` rate renders as "—" or "insufficient data", never as 0%.
2. When `insufficient_sample` is true, show counts only, or the rate together with its CI and a sample warning.
3. Never sum `model_performance` and `qualified_betting`. They answer different questions.
4. Never label the system profitable from these fields. `profitability_claim` is `NONE`.
5. Show `excluded_from_headline` next to the headline so the forward-only filter is visible.

## Versioning

- Additive fields keep `football_monitoring_v1`.
- Renamed or removed fields bump the version. Football will serve the old version alongside the new one for one release.
- The production contract test (`test_football_client_api_token_production.py`) pins the top-level keys Tennis consumes.
