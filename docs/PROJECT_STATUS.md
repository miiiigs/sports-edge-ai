# Project Status

Last updated: Saturday, August 15, 2026

## Current phase

August 15, 2026 PDF-first football migration foundation.

The repository is still the active migration workspace, but the governing direction is now the
new football specification set from Saturday, August 15, 2026. The immediate objective is no
longer more `1X2` tuning. It is to establish a reproducible, auditable foundation for the new
football GAP architecture before any destructive football/tennis split.

## Current operating goal

Advance the football build along this sequence:

`chronological shots GAP -> validate state/predictions -> SOT GAP -> corners GAP -> walk-forward lambda methodology -> downstream conversion`

That means:

- treat the three August 15, 2026 PDFs as the primary authority after direct user instructions
- use the companion Markdown files only where they do not conflict with the PDFs
- preserve existing Poisson and watchlist work as baseline research rather than the main build path
- keep football and tennis decoupled from this point forward, but do not split repositories yet
- avoid threshold tuning, staking implementation, and live market expansion until the new football
  data/model contract is stable

## Completed in the current migration iteration

- verified the full suite before this iteration at `91 passed`
- verified the full suite after the chronological shots GAP experiment work at `100 passed`
- confirmed the intended developer workflow from `code/pyproject.toml`:
  - install with `python -m pip install -e ".\code[dev]"`
  - then run `pytest -q` from the repo root
- verified the current repo state at:
  - `46` Python modules under `code/src/sports_ai`
  - SQLite-backed research and ledger storage as the active implementation
  - football model surface still centered on Poisson `1X2` v1-v4 plus watchlist/paper-shadow
  - tennis model surface still limited to lightweight Elo and simulation scaffolding
- completed the repository-to-new-spec gap analysis in:
  - `research/spec_migration/NEW_SPEC_GAP_ANALYSIS_2026-08-15.md`
- implemented the football GAP historical data-readiness report in:
  - `code/src/sports_ai/experiments/football_gap_data_readiness.py`
- generated the actual readiness artifacts from the research store:
  - `research/football_research/football_gap_data_readiness_2026-08-15.md`
  - `research/football_research/football_gap_data_readiness_2026-08-15.json`
- verified actual loaded football GAP field coverage for all currently loaded seasons
  `2122-2526` in:
  - `ENG1` / `E0`
  - `ENG2` / `E1`
  - `ENG3` / `E2`
  - `ENG4` / `E3`
  - `ESP1` / `SP1`
- confirmed that the currently loaded scope has complete stored-row coverage for:
  - home/away shots
  - home/away shots on target
  - home/away corners
- confirmed that the currently loaded scope is not the full football-spec archive:
  - expected scope entries: `65`
  - loaded scope entries: `25`
  - missing scope entries: `40`
  - not-yet-loaded competitions: `EFLC`, `FAC`, `FRA1`, `GER1`, `ITA1`, `SCO1`, `UCL`, `UEL`
- recorded that `SP1` referee coverage is incomplete in the current store, but that this does not
  block the GAP engine itself
- implemented the first generic football GAP ratings core in:
  - `code/src/sports_ai/agents/football/gap_ratings.py`
- kept the GAP core generic across supported statistics while using `shots` as the first
  practical instantiation
- implemented the first chronological football shots GAP experiment runner in:
  - `code/src/sports_ai/experiments/football_gap_shots_walkforward.py`
- generated the first reproducible shots GAP walk-forward artifacts:
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_manifest.json`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_predictions.jsonl`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15_final_state.json`
  - `research/football_research/football_gap_shots_walkforward_reference_2026-08-15.md`
- verified deterministic replay for the real loaded archive:
  - predictions match on repeated fresh runs
  - final state matches on repeated fresh runs
- verified the first real shots GAP run against the currently loaded scope:
  - matches processed: `12,080`
  - predictions produced: `12,080`
  - evaluation predictions after warm-up exclusion: `9,664`
  - skipped events: `0`
  - learning rate: `0.05` labeled `REFERENCE / UNOPTIMIZED`
- recorded the real shots diagnostics as mechanics-only research outputs:
  - overall evaluation combined MAE about `3.59535`
  - home mean error about `+0.31581` as observed-minus-expected
  - away mean error about `-0.20575` as observed-minus-expected
- added hard invariants and correctness tests for:
  - hand-computed GAP updates
  - chronological isolation
  - statistic isolation
  - league isolation
  - home/away state isolation
  - missing-input failure
  - prohibition on goals as GAP inputs
  - deterministic replay
  - initialization leakage isolation
  - stable kickoff/event ordering
  - season-transition/new-team observation counts
  - readiness-gated walk-forward scope
  - duplicate-event rejection

## Active migration workstream

- keep the August 15, 2026 PDF-first authority order as the implementation guardrail
- keep current football development centered on GAP and related readiness/state contracts
- use the current repo as transitional infrastructure until dependency mapping is complete
- maintain auditable docs for blockers, decisions, and traceability as classifications change

## BASELINE / LEGACY RESEARCH

These assets remain preserved, but they are no longer the immediate development priority:

- football Poisson `1X2` v1-v4
- the older `1X2` decision-layer and threshold experiments
- the current football watchlist/live review flow
- historical close-vs-close football signal studies
- previous Poisson finalization and optimizer diagnostics

This work is still useful for benchmarking, reusable Dixon-Coles arithmetic, and migration
reference. It should not quietly become the default football product direction again.

## Current constraints

- `00-developer-brief.md` is still missing, so `R1-R10` and shared arithmetic/staking references
  remain unresolved
- the supplied PDFs and companion Markdown files are not identical, so requirement conflicts must
  continue to be recorded explicitly
- the current repository/runtime/database are still shared across football and tennis even though
  the target architecture requires separation
- the currently loaded football research archive covers only `ENG1`, `ENG2`, `ENG3`, `ENG4`, and
  `ESP1`, not the full football-spec competition scope
- `SP1` referee coverage is incomplete in the current store, which limits later
  cards/referee-dependent veto work for that league
- no resolved production sharp-reference live feed exists yet
- no final PostgreSQL deployment design should be assumed yet

## Immediate next actions

1. keep the new football GAP data contract stable and auditable
2. review the first chronological shots artifacts and confirm the state/prediction contract is sufficient
3. run the same chronological framework for `shots_on_target`
4. then run the same framework for `corners`
5. postpone lambda fitting, downstream conversion, pricing, staking, and repo-splitting work until football dependencies and state
   contracts are clearer

## Current bottom line

The migration foundation is now materially cleaner than it was before this pass.

The repo now has a reproducible editable-install test workflow, an actual football GAP
data-readiness report against the SQLite research store, a tested generic GAP core, and the first
deterministic chronological shots GAP replay over real historical matches. The next football work
should continue from that state/prediction foundation rather than drifting back into Poisson
optimization or early betting-market feature expansion.
