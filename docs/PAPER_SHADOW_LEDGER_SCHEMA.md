# Paper / Shadow Ledger Schema

Last updated: Thursday, August 13, 2026

This is the early immutable history structure for trial outputs.

## Purpose

The client wants early trial history.

That history must:

- be timestamped;
- store the available price at decision time;
- not be rewritten after the result is known;
- support CLV and later evaluation.

## Minimum Fields

- `decision_id`
- `created_at`
- `run_id`
- `event_id`
- `sport`
- `competition`
- `market`
- `selection`
- `line`
- `available_price`
- `bookmaker`
- `fair_probability`
- `fair_odds`
- `market_probability`
- `edge`
- `data_quality`
- `uncertainty`
- `decision`
- `decision_reason`
- `model_version`
- `strategy_version`
- `policy_version`
- `result_status`
- `closing_price`
- `settlement`
- `clv`
- `notes`

## Result Status Values

- `PENDING`
- `SETTLED`
- `VOID`
- `CANCELLED`

## Decision Values

- `PASS`
- `WATCH`
- `BET_CANDIDATE`

## Hard Rules

- do not overwrite the original decision row;
- later settlement should append or update only the allowed post-event fields;
- store the exact available price at decision time;
- if price is unknown, record `UNKNOWN`, not a guess;
- preserve version information for reproducibility.
