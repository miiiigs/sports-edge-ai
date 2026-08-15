# Football Research

Date created: 2026-08-13

This workspace exists because football is now an immediate parallel workstream, not just a later-phase idea.

## Immediate Goal

Begin football research and data preparation now while MLB reverse engineering continues.

## Current Direction

The meeting notes point to:

- singles-first output format;
- likely near-term interest in BTTS and match-result style markets;
- additional research interest in corners and cards;
- strong importance of odds and market comparison;
- CLV-first evaluation rather than superficial win rate.

## Current Source Direction

- `football-data.co.uk` for free historical CSV-based football research data
- `API-Football` as the current leading live/current-season API candidate
- `football-data.org` as a useful secondary option, but not the main historical betting-data solution for the meeting's stated need

## What We Confirmed Already

- the free historical football path is usable now
- 2025/2026 validation succeeded for `E0`, `E1`, `E2`, `E3`, and `SC0`
- those validated files include results, cards, corners, and multiple closing-odds fields
- some 2026/2027 files already load, but current-season coverage is not yet uniform across all target leagues
- this means we can start the football historical setup without waiting for a paid API

See:

- `field_validation_2026-08-13.md`
- `source_plan.md`
- `intake_foundation_2026-08-13.md`
- `market_validation_experiment.md`

## What Exists Now

- first historical football artifact export for `ENG1 2526`
- first local football research store at `research/football_research/store/football_research.sqlite`
- first market-validation logging template for a narrow manual or semi-structured experiment
- first known-league stored foundation now includes:
  - Premier League `ENG1 2526`
  - La Liga `ESP1 2526`

## Immediate Work In This Folder

- source assessment
- league/market scope
- field requirements
- future ingestion notes
- validation evidence
- intake foundation notes
- local stored data
- market-validation definition
