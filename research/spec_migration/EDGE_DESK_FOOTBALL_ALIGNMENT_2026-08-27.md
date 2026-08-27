# Football Edge Desk Alignment

Date: `2026-08-27`

## Purpose

This document records the truthful Football Edge Desk state after the Route A validation remediation completed on Thursday, August 27, 2026.

It preserves the earlier Phase 2A implementation history while correcting the model-validity status.

## Current Agent State

- `ROUTE_B_COMPOSITE`: `ACTIVE`
- `ROUTE_A_DIRECT_CLASSIFIER`: `REJECTED`
- Agent 3: not implemented

Route A remains registered in the shadow architecture so blocked and rejected states are recorded honestly, but it is not an active runtime voting agent.

## Route A Remediation Outcome

The original Route A validation is now superseded and invalidated because:

- Day 7 GAP features were previously rebuilt with learning-rate choices informed by later seasons
- calibration method selection previously consulted outer test performance

The corrected Route A research path now:

- excludes the Day 7 warm-up season `2122` from Route A evidence
- rebuilds historical shots and corners features with target-season-specific nested GAP fitting
- predicts same-kickoff fixtures from a shared pre-batch state before any same-time updates are applied
- freezes calibration choices on dedicated calibration seasons before untouched outer tests
- loads Route A for runtime only through a manifest-pinned artifact with schema checks and SHA-256 verification

## Corrected Validation State

The clean Route A artifact dated `2026-08-27` rejected both candidate markets:

- `BTTS`: `REJECTED`
- `goals_total 2.5`: `REJECTED`

The committed manifest therefore records:

- `validation_status = FAILED`
- `review_status = PENDING_INDEPENDENT_REVIEW`
- `overall_route_a_status = REJECTED`

This means the Edge Desk runtime must not count Route A as a second voter on either overlapping market.

## Consensus Truth

The named Route A plus Route B consensus profile still exists structurally:

- config id: `football_route_a_b_consensus`
- version: `FOOTBALL_ROUTE_A_B_V1`
- rule: same direction required
- spread gate: `<= 0.06`

Because the current Route A artifact is rejected, those overlapping markets remain truthful one-active-agent contexts at runtime:

- `BTTS`: insufficient active quorum
- `goals_total 2.5`: insufficient active quorum
- `1x2`, other totals, corners, and Asian handicap: unchanged Route B-only contexts

## Immutable Ledger State

Phase 1A protections remain intact:

- immutable agent registry
- immutable market-capability history
- immutable agent predictions
- immutable consensus history
- immutable decision history

The remediation added pending-review and rejected capability states so Route A can be recorded accurately without silent activation.

## Route B Preservation

The remediation did not change Route B mathematics:

- GAP math: unchanged
- Day 8 xG conversion: unchanged
- Day 9 Dixon-Coles math: unchanged
- Day 10 pricing math: unchanged
- current market / candidate / paper flow: unchanged

## Summary

Football Edge Desk Phase 2A remains structurally integrated, but as of Thursday, August 27, 2026 there is still only one active Football predictive agent in runtime:

- `ROUTE_B_COMPOSITE`

Remaining later-phase work is unchanged:

- independent re-review of the exact Route A remediation artifact
- any future Route A approval or replacement
- Agent 3 design
- real three-agent consensus
- value integration
- risk integration
- dashboard presentation improvements

## Phase 3A Best Picks Dashboard

Phase 3A adds a picks-first main dashboard view without changing model math or re-running qualification logic in the UI.

- Route B remains `ACTIVE`
- Route A remains `REJECTED`
- Agent 3 remains not implemented
- the main dashboard now prioritizes qualified current-market picks with human-readable market labels
- blocked, rejected, and no-bet rows remain available in deeper views instead of cluttering the main board
- the external `Edge-Desk-Design-Pack.html` reference was not present in the repository or nearby project reference roots at implementation time, so the dashboard was refocused using the existing Football dashboard visual language rather than a new redesign

Existing persisted fields already available for the next performance-tracking phase include:

- `fixture_id`, `kickoff`, competition, home team, and away team context
- `market`, `selection`, and `line`
- `model_probability` and `model_fair_price`
- `available_market_price`, `bookmaker`, `entry_snapshot_id`, and captured timestamps
- `edge`, `minimum_acceptable_price`, decision status, and safeguard state
- paper-trade ledger state including open and settled positions
- decision history and Edge Desk shadow consensus history

The next dashboard phase should focus on pick settlement, hit rate, ROI, and performance tracking rather than more presentation-layer qualification work.
