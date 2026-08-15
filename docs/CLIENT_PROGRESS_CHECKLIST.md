# Client Progress Checklist

Last updated: Friday, August 14, 2026

This version is written in simple client-friendly language.

## Done

- The project is now a real runnable Python codebase, not only documents.
- The recovered codebase is passing `52` automated tests.
- We rebuilt the Pack-style starter structure instead of inventing a different architecture.
- We created a local football research database and separated football match history from market quote history.
- We loaded historical football data for:
  - Premier League
  - Championship
  - League One
  - League Two
  - La Liga
- The football research store now contains:
  - `2,416` football matches
  - `8,249` football market quote rows
- We built the first blind football Poisson `1X2` model as an interpretable baseline.
- We built the football watchlist pipeline for upcoming fixtures.
- We added prediction-lock storage before market comparison.
- We added market-lock storage and append-only market snapshots for observed prices.
- We changed live upcoming football outputs to a clearer paper-trial label:
  - `PAPER_SHADOW_CANDIDATE`
- We separated the historical close-vs-close football experiment from live picks:
  - it now logs as `RESEARCH_SIGNAL`
- We built the paper/shadow ledger, grading flow, static dashboard, and persistent review app.
- We continued organizing the MLB reverse-engineering work as a separate research track.

## Doing now

- Verifying the exact Saturday, August 15, 2026 football fixtures we should target.
- Checking which current bookmaker prices are available and usable for those fixtures.
- Tightening the opening-week football caution rules so the system stays conservative.
- Preparing the watchlist/review flow so results are easier to inspect and explain.

## Next

- Capture current prices with bookmaker, source, and timestamp.
- Run the football watchlist using all `1X2` outcomes, not only home selections.
- Rank any qualifying results by edge, uncertainty, and data quality.
- Publish up to two paper/shadow trial candidates if they genuinely qualify.
- If nothing qualifies, publish none and keep the audit trail honest.
- Append the closing price later.
- Append the final result later.
- Judge the trial first by:
  - whether the prediction was blind before market comparison
  - whether the price was captured properly
  - whether the record was locked before kickoff
  - whether the closing market later supports the value claim

## Important notes

- Saturday, August 15, 2026 should be treated as a paper/shadow trial, not proof of a validated production edge.
- The system should never force exactly two picks just to satisfy a target count.
- A result can still be useful even if it loses, as long as the process was honest and the price evaluation is good.

## Current bottom line

- The project is materially stronger than the older repo notes suggested.
- The codebase, football intake, ledger, and review tooling are real and working.
- The next goal is not to promise winners.
- The next goal is to produce an honest, timestamped, auditable football trial process for Saturday, August 15, 2026.
