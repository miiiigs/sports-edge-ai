# Route A Implementation Contract

Date: `2026-08-27`

## Source Of Truth Used

- `initial_data/football-build-spec.md`
- `initial_data/developer-setup-costs-and-30-day-plan.md`
- `docs/DECISION_LOG.md`
- `docs/EXPERIMENT_LOG.md`
- `docs/REQUIREMENTS_TRACEABILITY.md`
- `research/spec_migration/NEW_SPEC_GAP_ANALYSIS_2026-08-15.md`
- `research/spec_migration/FULL_SPEC_ALIGNMENT_AUDIT_2026-08-17.md`
- `research/spec_migration/EDGE_DESK_FOOTBALL_ALIGNMENT_2026-08-27.md`
- accepted Day 7, Day 8, Day 9, Day 10 football code paths

## Reconstructed Route A Contract

- methodology: direct classification
- baseline model family: logistic regression
- accepted feature baseline:
  - GAP-predicted home shots
  - GAP-predicted away shots
  - GAP-predicted home corners
  - GAP-predicted away corners
  - shot sum
  - shot difference
  - corner sum
  - corner difference
  - competition identity
- targets:
  - `BTTS`
  - `Over 2.5`
- supported runtime markets:
  - `btts`
  - `goals_total` with line `2.5`

## Deliberate Non-Expansions

- `shots_on_target` was not added to the Route A baseline because repository evidence did not justify making it part of the original direct-classification contract.
- no `1x2`, corners, or Asian handicap support is claimed for Route A.
- no Agent 3 was created.
- no Route B pricing or candidate math was rewritten.

## Training And Validation Rules

- dataset source: Day 7 selected GAP walk-forward predictions joined to realized outcomes from the research store
- chronology rule: `as_of <= kickoff` enforced for every training row
- split strategy: chronological expanding-window walk-forward by season
- naive baseline: point-in-time-safe league competition base rate
- calibration policy:
  - evaluate uncalibrated logistic first
  - evaluate held-out Platt when at least two prior seasons exist
  - evaluate held-out isotonic only when held-out calibration sample size is sufficient
  - allow `none` when calibration does not improve held-out evidence

## Activation Gate

Route A is allowed to register active only if both intended markets credibly improve on the naive baseline in out-of-sample log loss and Brier score.

Otherwise Route A must remain inactive and truthfully block in runtime.

## Consensus Contract

When Route A is active, the overlapping Route A plus Route B shadow consensus profile is:

- config id: `football_route_a_b_consensus`
- version: `FOOTBALL_ROUTE_A_B_V1`
- same direction required
- probability spread must be `<= 0.06`
- evaluated per market context, not per stored selection row

## Runtime Storage Contract

Every real Route A runtime output is stored in the existing immutable Edge Desk ledger with:

- fixture identity
- agent identity and version
- market family and line
- selection
- probability
- fair price
- `as_of`
- model version
- prediction status
- block reason when applicable
