# Football Field Validation

Validation date: 2026-08-13

This file records what we actually checked from `football-data.co.uk` so the
football plan is based on evidence, not assumptions.

## What Was Checked

- historical 2025/2026 files for `E0`, `E1`, `E2`, `E3`, `SC0`
- current 2026/2027 files for `E0`, `E1`, `E2`, `E3`, `SC0`
- official `notes.txt` field definitions from `football-data.co.uk`

## Historical Validation Result

The checked 2025/2026 files loaded successfully for:

- `E0` Premier League
- `E1` Championship
- `E2` League One
- `E3` League Two
- `SC0` Scottish Premiership

Observed strengths in those files:

- match rows present
- results fields present
- corners fields present: `HC`, `AC`
- cards fields present: `HY`, `AY`
- Bet365 closing odds present: `B365CH`, `B365CD`, `B365CA`
- Pinnacle closing odds present: `PSCH`, `PSCD`, `PSCA`

This means the free historical source is strong enough to begin football
historical intake and early backtest preparation right now.

## Current-Season Validation Result

The checked 2026/2027 files showed mixed availability:

- `E0` loaded successfully
- `E3` loaded successfully
- `SC0` loaded successfully
- `E1` returned `HTTP 300`
- `E2` returned `HTTP 300`

For the current files that did load:

- rows were present
- corners and cards fields were present
- Bet365 closing odds were present
- Pinnacle closing-odds fields were not present in the checked files
- total column count was lower than the 2025/2026 historical files

This tells us the free source is enough for bootstrap work, but current-season
coverage should be treated as partially reliable until we finish league-by-league
 validation.

## Practical Decision

Use this source split for now:

- historical football bootstrap: `football-data.co.uk`
- current/live football evaluation: `API-Football`

## When A Paid API Is Actually Needed

You do **not** need a paid football API to start the football historical setup.

A paid or live API becomes important when we want to do one or more of these:

- daily current-season fixture ingestion
- more reliable current-league coverage across all target divisions
- lineups close to kickoff
- injuries/suspensions
- current odds snapshots
- live or near-live reruns before matches

## Immediate Engineering Use

The repo now contains a small helper module that can profile one
`football-data.co.uk` CSV at a time:

- `code/src/sports_ai/data_sources/football_data_co_uk.py`

This is the first practical football intake building block.
