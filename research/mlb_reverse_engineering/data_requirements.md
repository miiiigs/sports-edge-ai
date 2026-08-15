# MLB Reverse Engineering Data Requirements

Date created: 2026-08-13

## Goal

For each of the 22 visible plays, reconstruct the event and market context honestly enough to support hypothesis testing without hindsight leakage.

## Minimum Reconstruction Targets Per Pick

- exact date
- exact matchup
- venue
- starting pitchers
- projected lineup
- confirmed lineup
- injuries/availability
- bullpen availability
- team form and workload
- weather
- park factor
- opening line/odds
- pick-time line/odds
- pick timestamp
- post-pick movement
- closing line/odds
- sportsbook
- market consensus
- ticket percentage if reliable
- money percentage if reliable
- final result

## Required Provenance Structure

Every reconstructed field should eventually carry:

- `value`
- `source`
- `source_timestamp`
- `reconstruction_confidence`
- `known_at_pick_time`

## Confidence Guidance

- `LOW`: inferred only from a partial artifact, or multiple candidate events remain
- `MEDIUM`: likely event identified but not all point-in-time details proven
- `HIGH`: event and key context supported by multiple timestamped sources

## Status Guidance

- `UNRESOLVED`
- `CANDIDATE_EVENT_FOUND`
- `EVENT_CONFIRMED`
- `MARKET_CONTEXT_PARTIAL`
- `MARKET_CONTEXT_CONFIRMED`

## Hard Rules

- Do not fabricate unknown dates, sportsbooks, prices, pitchers, lineups, or timestamps.
- Do not backfill post-game or late-breaking data into pick-time fields.
- Do not treat betting-share screenshots or marketing claims as validated market data without provenance.
- If a field cannot be reconstructed reliably, keep it `UNKNOWN`.
