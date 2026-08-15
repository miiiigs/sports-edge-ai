# MLB Reverse Engineering

Date created: 2026-08-13

This directory holds the research-side source of truth for the MLB 22-pick sample shown in `initial_data/bets_referral.png`.

## Purpose

The MLB sample is research evidence only.

It is not currently approved as automatic production V1 scope.

The goal here is to:

1. normalize the 22 listed plays into structured records;
2. reconstruct the exact historical events and point-in-time context;
3. convert bettor/bookmaker claims into explicit hypotheses;
4. identify which variables are testable with reliable historical data;
5. support future backtesting without contaminating Zone A blind features.

## Current Source Artifacts

- `picks_22.csv`: normalized list of the 22 visible plays from the screenshot.
- `reconstruction_status.csv`: per-bet reconstruction progress tracker.
- `HYPOTHESES.md`: formalized strategy hypotheses derived from the voice-note and sample.
- `data_requirements.md`: MLB-specific reconstruction requirements and provenance rules.
- `source_registry.md`: MLB-specific source candidates and evidence notes.
- `experiments/`: placeholder for future reverse-engineering experiments and reports.

## Ground Rules

- Unknown fields stay `UNKNOWN`.
- Dates are never guessed.
- Odds are never invented.
- Every reconstructed field should eventually carry provenance.
- `known_at_pick_time` is mandatory when historical context is added.
- Market-derived information belongs in market/research layers, not Zone A blind features.
