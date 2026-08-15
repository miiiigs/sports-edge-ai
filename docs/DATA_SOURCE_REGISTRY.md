# Data Source Registry

Last updated: 2026-08-13

This is the global registry for provider evaluation.

## Confirmed local artifacts

| Source | Scope | Role | Notes |
| --- | --- | --- | --- |
| `initial_data/Sports-AI-Developer-Pack-V1(2).pdf` | architecture | Pack reference and code-recovery evidence | not a live or historical market feed |
| `initial_data/bets_referral.png` | MLB research | screenshot source for the 22-play sample | not sufficient for date/book/odds reconstruction |

## Current football source candidates

| Source | Scope | Role | Notes |
| --- | --- | --- | --- |
| `football-data.co.uk` | football historical research | first-choice free historical CSV bootstrap | official site presents free historical results and odds data in CSV/Excel form; strong immediate fit for football backtesting foundations |
| `API-Football` | football live/current-season data | current leading live API candidate | official pricing currently shows `Free = 100 requests/day` and `Pro = $19/month = 7,500 requests/day`; good candidate for fixtures, lineups, injuries, stats, and current odds |
| `football-data.org` | football API alternative | secondary current-data option | useful machine-readable football API, but less directly aligned than the other two for the exact free historical betting-data need |

## Football validation summary

Validated on Thursday, August 13, 2026:

- `football-data.co.uk` 2025/2026 files loaded successfully for `E0`, `E1`, `E2`, `E3`, and `SC0`
- those checked files included results, corners, cards, and key closing-odds fields
- some 2026/2027 files already loaded successfully, but not all target divisions behaved consistently on the validation date

Practical consequence:

- we can start football historical setup now without a paid API
- we should expect to need a live/current API before dependable current match-day workflows

## Provider categories still to evaluate

| Category | Needed for | Point-in-time critical? | Status |
| --- | --- | --- | --- |
| tennis historical stats | ATP minimum honest loop | yes | pending |
| football historical stats | immediate football research plus future football build | yes | partially clarified |
| historical odds snapshots | market layer and MLB reconstruction | yes | pending |
| line movement history | M03-style logic and voice-note testing | yes | pending |
| lineup/injury feeds | sports context reconstruction | yes | pending |
| weather archives | totals and environment context | yes | pending |
| betting-share data | line-resistance hypotheses | yes | unverified |
