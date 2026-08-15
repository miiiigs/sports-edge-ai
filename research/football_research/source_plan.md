# Football Source Plan

Date created: 2026-08-13

## Historical / Backtest-Friendly Source

### `football-data.co.uk`

Role:

- first-choice historical football source for immediate research and backtesting bootstrap.

Why it is strong:

- free downloadable CSV files;
- long historical archive;
- no scraping required;
- suitable for Python/pandas workflows;
- aligns well with early BTTS/goals/cards/corners exploration.

What it appears useful for:

- results
- goals
- cards
- corners
- shots
- closing odds from multiple books

Current use recommendation:

- use this first for football historical research and backtesting foundations.

Current caution:

- confirm exact field consistency across every target division before locking assumptions;
- closing odds are useful, but they are not a full timestamped line-movement history;
- lower-division depth and consistency still need validation for some fields.

Validation evidence now recorded:

- 2025/2026 checks succeeded for `E0`, `E1`, `E2`, `E3`, `SC0`
- all of those checked files included results, corners, cards, and key closing-odds columns
- some 2026/2027 files already load, but not all target divisions behaved consistently on the check date

Practical conclusion:

- this source is good enough to start football historical setup immediately
- it is not yet enough to assume clean full current-season operational coverage across every target league

## Live / Current-Season Source

### `API-Football`

Role:

- leading current candidate for live/current football data.

Why it is useful:

- fixtures
- live scores
- events
- lineups
- injuries
- statistics
- pre-match odds
- in-play odds

Current caution:

- request budget matters;
- lower-league detail still needs validation;
- corners/cards/statistics quality should be checked before operational reliance.

Current use recommendation:

- evaluate for current-season football intake and lineups/statistics support;
- do not assume uniform quality across all lower leagues without testing.

When it becomes necessary:

- once we want daily current fixtures, lineups, injuries, or current odds in a dependable workflow
- once we need more reliable current coverage than the free CSV path provides
- once we begin pre-match rerun logic close to kickoff

## Secondary / Alternative Option

### `football-data.org`

Role:

- useful developer-friendly football API;
- secondary option, not current first choice for the exact historical betting-data need described in the meeting.

Why:

- useful machine-readable football data;
- free top competitions;
- may be helpful for current fixtures/tables/squads;
- less directly aligned than `football-data.co.uk` for free historical betting-model bootstrap.

## Immediate Decision

Use this practical split:

- historical football research: `football-data.co.uk`
- current/live football API evaluation: `API-Football`
- secondary fallback / comparison option: `football-data.org`

## Current API Timing

Right now:

- no paid football API is required to begin the football historical setup

Sooner rather than later:

- a live/current API should be obtained before we try to operationalize current match-day football workflows
