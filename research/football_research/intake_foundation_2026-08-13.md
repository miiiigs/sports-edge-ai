# Football Intake Foundation

Date created: 2026-08-13

This records the first real football build step after source validation.

## What Was Built

A new intake module was added at:

- `code/src/sports_ai/data_sources/football_history.py`

It can:

- download a historical football CSV from `football-data.co.uk`
- normalize each match into a canonical event row
- create a separate market row set for closing 1X2 prices
- produce a coverage summary for the league-season file

## Why This Matters

This is the first practical football data spine.

It gives the project a reusable way to start from real football data while
keeping sports/event information separate from market/odds information from the
beginning.

## Current Output Shape

The module currently produces:

- sports rows
- market rows
- coverage summary
- compact CLI preview output

## Important Limitation

This is an intake foundation, not a final modeling layer.

It does **not** yet:

- create pre-match feature sets
- create database tables
- create prediction locks
- create market locks
- run football models

## Verification

Verified on 2026-08-13:

- automated tests passed
- live preview run succeeded on `2526/E0`
- first exported artifact generated at `research/football_research/artifacts/ENG1_2526`
- first local football research store populated at `research/football_research/store/football_research.sqlite`
