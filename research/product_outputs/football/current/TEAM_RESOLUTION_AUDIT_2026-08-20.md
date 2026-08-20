# Team Resolution Audit

Date: Thursday, August 20, 2026

## Scope

This audit uses the stored local football-data.org fixture snapshot captured from Wednesday, August 19, 2026.

The verified local environment on Thursday, August 20, 2026 does not expose `SPORTS_AI_FOOTBALL_DATA_TOKEN`,
so this is a replay audit of real stored current fixtures rather than a fresh live API sync.

## Before

- resolved: `47`
- unresolved: `17`
- prediction ready: `18`
- prediction blocked: `33`

These are the prior Railway production counts supplied for the live dashboard state before the V1.1 hardening pass.

## After

- fixtures discovered from stored snapshot replay: `46`
- supported fixtures: `29`
- team resolution: `49 resolved / 0 unresolved / 7 model-history-missing`
- prediction coverage: `21 ready / 25 blocked`

## Alias / Normalization Gains

Deterministic provider aliases now resolve:

- `Brighton Hove -> Brighton`
- `Ipswich Town -> Ipswich`
- `Leeds United -> Leeds`
- `Nottingham -> Nott'm Forest`
- `Derby County -> Derby`
- `Preston NE -> Preston`
- `Atleti -> Ath Madrid`
- `Barça -> Barcelona`
- `Coventry City -> Coventry`
- `Hull City -> Hull`
- `West Ham -> West Ham`
- `Wolverhampton -> Wolves`
- `Bolton -> Bolton`
- `Lincoln City -> Lincoln City`
- `Santander -> Racing Santander`

Safe normalization was also tightened so names such as `Derby County` and `Real Racing Club de Santander`
are no longer corrupted by over-aggressive substring removal.

## Remaining Honest Blockers

The replay no longer leaves supported-current teams in a raw identity-unresolved state.

The remaining blockers are genuine missing current competition model history for:

- `Coventry City` in `ENG1`
- `Hull City` in `ENG1`
- `Lincoln City` in `ENG2`
- `West Ham` in `ENG2`
- `Wolverhampton` in `ENG2`
- `Bolton` in `ENG2`
- `Santander` in `ESP1`

Each of those sides is now classified as:

- identity resolved
- current competition history missing across `shots`, `shots_on_target`, and `corners`
- prediction blocked honestly instead of backfilling synthetic baseline strength

## Acceptance Notes

- supported-current identity failures on the stored snapshot were reduced from unresolved-name failures to `0`
- unresolved supported-current teams were not faked into baseline predictions
- the remaining blocked rows are now explicit model-history blockers, not name-resolution bugs
- this closes the bounded team-resolution hardening goal for the available local current-fixture evidence on Thursday, August 20, 2026
