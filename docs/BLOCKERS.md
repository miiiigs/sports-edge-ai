# Blockers

Last updated: Thursday, August 20, 2026

## Resolved / superseded

- Resolved on Monday, August 17, 2026: the repository contains the authoritative
  `initial_data/00-developer-brief.pdf`, so the earlier blocker that treated the missing
  `00-developer-brief.md` mirror as a specification gap is superseded. The PDF itself defines
  `R1-R10`, and the project now uses the PDF-first authority order. The missing Markdown mirror is
  a documentation convenience issue, not a live implementation blocker.

## Technical

- the supplied PDFs and companion Markdown files are not identical, so requirements must continue
  to be resolved through the PDF-first authority order rather than merged casually
- the final football/tennis split architecture is still unresolved at the dependency-map level, so
  the current repo remains a temporary migration workspace
- the live production PostgreSQL design is still unresolved and should not be assumed from the
  current SQLite research implementation
- Day 11A itself is now implemented with settlement-aware edge arithmetic, but any later shared
  `model_probability` or generic edge field used beyond the current manager-core layer must still
  avoid silently pretending push and quarter-line markets are ordinary binary events
- the new historical benchmark now answers calibration and market-specific performance honestly, but
  a later live paper layer still must not collapse settlement-aware markets back into naive binary
  probability semantics when reporting edge or profitability

## Data

- the football historical scope required by the PDFs is not fully loaded yet:
  missing competitions in the current research store are `EFLC`, `FAC`, `FRA1`, `GER1`, `ITA1`,
  `SCO1`, `UCL`, and `UEL`
- canonical multi-provider football team identity / alias mapping is not implemented yet
- `SP1` referee coverage is incomplete in the current loaded scope; this does not block GAP field
  readiness, but it does limit later referee/cards/veto functionality for that league
- current forward-production football parameter snapshots are not resolved yet:
  the loaded Day 8 conversion coefficients, Day 8 corner-distribution parameters, and Day 9 rho
  values are valid historical walk-forward artifacts, but they are not automatically a present-tense
  2026-27 production fit for paper predictions
- the historical performance benchmark is now complete, but genuine historical entry-odds timing is
  still unavailable in the frozen archive, so historical CLV and true decision-time ROI remain
  unproven rather than partially solved
- Day 12 safeguard code is now implemented, but the current repository still does not contain a
  connected point-in-time-valid pre-match xG input path for the competitions where the football
  spec requires that check; Day 12 therefore remains `PARTIAL` honestly rather than being
  overstated as complete

## Provider / operations

- the active dashboard provider is now `football-data.org`, but the verified local environment on
  Wednesday, August 19, 2026 does not currently set `SPORTS_AI_FOOTBALL_DATA_TOKEN`
- because of that verified configuration gap, the dashboard currently reports the fixture feed as
  `NOT_CONFIGURED` and cannot yet validate a first real current football-data.org fixture flowing
  into a persisted current prediction through the live dashboard path
- the new normalized current market-provider contract is implemented, but no real current odds
  provider is configured yet, so the system correctly reports `CURRENT_ODDS_PROVIDER =
  NOT_CONFIGURED` and `CURRENT_MARKET_DECISIONS = BLOCKED`
- no resolved sharp-reference live feed is connected yet for the football migration target
- lineup, injury, manager-change, and other veto-layer context feeds are still unresolved
- no approved production odds-snapshot schedule or final football live-runtime design is in place
- the live paper pipeline still cannot be marked ready even though the Day 11A manager core is now
  complete and the paper-ledger foundation now exists, because current/live odds-provider input,
  current forward-fit football model snapshots, and the xG dependency remain unresolved

## Client confirmation

- whether the team wants a Markdown mirror of `00-developer-brief.pdf` for convenience, even though
  it is no longer a specification blocker
- whether quarter-Kelly or half-Kelly is authoritative once the broader staking-layer work resumes
- which sharp-reference provider should be treated as authoritative for football live scanning
- when the football/tennis repository and database split should be executed after dependency mapping

## Practical meaning

These blockers do not prevent continued football GAP migration work inside the current repository.
They do prevent the project from honestly claiming that the full football-spec archive, provider
stack, final deployment architecture, or later paper-candidate path are already settled.

The Day 11A and validation-build iterations did not reopen the closed Day 7 through Day 10 model
layers. They closed the provider-independent manager core, benchmark, and paper-tracking
foundation honestly and separately recorded:

- `DAY11_MANAGER_CORE_READY = YES`
- `HISTORICAL_BENCHMARK_COMPLETE = YES`
- `PROBABILITY_CALIBRATION_MEASURED = YES`
- `PAPER_TRADING_LEDGER_READY = YES`
- `LIVE_PAPER_PIPELINE_READY = NO`

That remaining `NO` status is now driven mainly by:

- unresolved live/provider odds input
- unresolved as-of-current forward-production Day 8 and Day 9 football parameters
- unresolved Day 12 xG data source / point-in-time-validity dependency
- unresolved true decision-time entry-price and close-price pair capture for live CLV proof

The historical validation layer is now closed for the currently loaded football scope, but live
paper output still depends materially on the unresolved archive-scope, live-odds, sharp-reference,
provider, current-fit, xG, and deployment items listed here.
