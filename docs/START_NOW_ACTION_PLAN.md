# Start Now Action Plan

Last updated: Friday, August 14, 2026

This is the simple version of what we should start now and why.

## Today

### 1. Pack starter code recovery

Why:

- right now the repository is mostly documents;
- the Pack already gives us the right safety structure;
- we should not invent a different system before trying to recover the intended one.

What this gives us:

- a real project structure;
- the firewall rules;
- prediction locking;
- market locking;
- a base to run tests and later build the first prototype loop.

Where it comes from:

- `initial_data/Sports-AI-Developer-Pack-V1(2).pdf`
- especially the Pack code listings already assessed in `PACK_CODE_EXTRACTION_ASSESSMENT.md`

Status:

- completed as a first runnable baseline

### 2. Start matching the 22 MLB picks to real historical games

Why:

- the client wants to know why those picks were chosen;
- we cannot answer that unless we know the exact game, date, market, and context;
- otherwise we would only be guessing after the fact.

What this gives us:

- real historical examples;
- a way to test hypotheses honestly;
- the base for reverse engineering.

Where it comes from:

- `initial_data/bets_referral.png`
- `research/mlb_reverse_engineering/picks_22.csv`

### 3. Start football data intake in parallel

Why:

- the client wants football started immediately;
- `meeting_2` now gives us a practical source path;
- football is no longer just a later-phase idea.

What this gives us:

- historical football data for research and backtesting;
- a path to collect current-season data;
- a practical way to start BTTS/goals/corners/cards research.

Where it comes from:

- `meeting_notes/meeting_2.txt`
- `football-data.co.uk` for free historical CSVs
- `API-Football` for live/current-season API evaluation

Status:

- started
- first `ENG1 2526` historical artifact exported
- `ESP1 2526` validated and stored as part of the same known-league base

Current default known-league base:

- Premier League
- La Liga

## Next

### 4. Add the next persistence / ledger layer

This is now the strongest engineering follow-up after the recovered skeleton.

Why:

- the code baseline works;
- we now need the history and data layers that make it auditable outside the demo;
- this moves us closer to real research replay and shadow outputs.

Status:

- started
- first local football research store created for `ENG1 2526`

### 5. Build the first small prototype loop

This should be:

- sports/context view;
- fair-price/probability view;
- market/odds view;
- final decision view.

Why:

- this matches what the client actually described;
- it is much better than trying to build all 50 agents immediately.

### 6. Prepare the paper/shadow decision ledger

Why:

- the client wants early trial history;
- history must be timestamped and immutable;
- future trial picks should be recorded properly from day one.

### 6A. Add one narrow market-validation experiment

This is the best new addition from `meeting_3`.

What it should look like:

- one market family first;
- one small league set first;
- one sharp-reference versus retail comparison loop first;
- timestamped logging;
- grading against the close.

Why:

- it gives a low-cost proof-or-disproof path;
- it stops the project from drifting into broad unmeasured theory;
- it can reject weak ideas quickly.

Important:

- this should support the main build path;
- it should not replace the firewall, history, or structured research foundation.

Status:

- concept accepted from `meeting_3`
- logging template and experiment note created
- default starting choices now proposed:
  - Premier League
  - La Liga
  - 1X2
  - Pinnacle as sharp reference

## After That

### 7. Replay reconstructed cases through the small prototype

Why:

- that is how we find out which ideas survive contact with real data;
- it keeps us focused on evidence instead of storytelling.

## Simple Bottom Line

We are starting with:

1. the project frame;
2. the real historical examples;
3. the data sources;
4. then the smallest honest prototype.

We are not starting with:

1. polished UI;
2. 50 full agents;
3. unsupported pick generation;
4. anything that rewrites history after the fact.
