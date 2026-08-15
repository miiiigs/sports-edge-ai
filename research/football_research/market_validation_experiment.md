# Narrow Market Validation Experiment

Last updated: Friday, August 14, 2026

## Purpose

This is the first narrow market-validation lane inspired by `meeting_3`.

The goal is not to prove a full betting system.

The goal is to answer a smaller question:

- do off-market football prices appear often enough to matter?
- do they survive long enough to capture?
- do they produce positive CLV often enough to justify deeper build work?

## Scope For The First Test

Keep the first version narrow:

- one football market family
- one small league set
- one sharp-reference comparison rule
- one consistent logging structure

## Proposed First Structure

Start with:

- sport: football
- league set: Premier League and La Liga
- market family: 1X2
- sharp-reference book: Pinnacle
- retail comparison set: Bet365 plus one or two additional client-relevant retail books
- duration: 2 weeks

## Why These Defaults

These are the best starting defaults for the current project because:

- the leagues are globally known and easy to verify
- the project already has usable historical support for them
- 1X2 is cleaner for a first structured pricing experiment than jumping straight into niche market complexity
- Pinnacle is the strongest default sharp-reference benchmark from the meeting direction
- a small retail comparison set keeps the experiment measurable instead of noisy

## Hard Rules

- record the exact timestamp of the observed price
- record the exact book names
- record the exact event and market
- record the compared prices as seen, not reconstructed later
- record the close when available
- do not backfill missing values with guesses
- do not mark something as an edge unless the comparison rule said it qualified at the time

## Minimum Fields

- `tracking_id`
- `observed_at`
- `sport`
- `competition`
- `event_name`
- `event_date`
- `market_type`
- `selection`
- `line`
- `sharp_book`
- `sharp_price`
- `retail_book`
- `retail_price`
- `gap_pct`
- `qualifies_flag`
- `entry_price`
- `closing_price`
- `result`
- `clv`
- `notes`

## Success Criteria

This first lane is useful if it can show:

- the gaps occur often enough to track seriously
- the prices can actually be captured
- the CLV is meaningfully better than noise

If the gaps are rare or CLV is flat:

- that is still a useful result
- it means the project should not overinvest in that lane

## Current Status

- experiment principle accepted
- default starting choices now proposed:
  - Premier League
  - La Liga
  - 1X2
  - Pinnacle as sharp reference
  - small retail comparison set
- logging template created
