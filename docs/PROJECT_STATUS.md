# Project Status

Last updated: Tuesday, August 18, 2026

## Current phase

Tuesday, August 18, 2026 PDF-first football Day 10 closure completed.

The repository remains the active migration workspace, but the governing direction is still the
August 2026 football specification set. The football build has now crossed from the Day 7 GAP
statistics foundation, through the Day 8 expected-goals conversion layer and Day 9 scoreline layer,
into a deterministic Day 10 market-pricing layer for the currently loaded football scope.

## Current operating goal

Advance the football build along this controlled sequence:

`chronological shots GAP -> SOT GAP -> corners GAP -> walk-forward lambda fitting -> statistics-to-goals conversion -> corner-distribution comparison -> Dixon-Coles scoreline matrix -> Day 10 fair-price derivation`

That means:

- treat the August 2026 PDFs as the primary authority after direct user instructions
- use companion Markdown files only where they do not conflict with the PDFs
- preserve existing Poisson and watchlist work as baseline research rather than the main build path
- keep football and tennis decoupled from this point forward, but do not split repositories yet
- stop Day 10 at fair prices only, without adding odds ingestion, de-vig, thresholds, staking, manager logic, or recommendations

## Completed in the current migration iteration

- verified the full suite before this Day 10 pass at `155 passed`
- verified the full suite after this pass at `175 passed`
- verified the current repo state at:
  - `53` Python modules under `code/src/sports_ai`
  - `24` test files
  - `165` explicit `test_...` functions
- preserved the Day 7, Day 8, and Day 9 artifacts as the frozen upstream baseline
- added the Day 10 market-pricing module in:
  - `code/src/sports_ai/experiments/football_day10_market_pricing.py`
- added the Day 10 invariant suite in:
  - `code/tests/test_football_day10_market_pricing.py`
- kept the Day 9 -> Day 10 goals interface explicit:
  - Day 10 consumes the persisted Day 9 scoreline matrix directly
  - Day 10 does not rebuild goal probabilities from `expected_goals_home`, `expected_goals_away`, or `rho`
  - the Day 9 historical row contract was hardened to carry `predicted_home_corners` and `predicted_away_corners` forward for the Day 8 corner object
- kept the Day 8 -> Day 10 corners interface explicit:
  - Day 10 consumes predicted home/away corners, selected `poisson` or `negative_binomial`, and persisted dispersion directly
  - corner totals use adaptive support with explicit residual tail mass instead of a `0..10` truncation
- implemented:
  - a strict sanitized pre-match Day 10 pricing input that excludes realized outcomes and market data
  - deterministic scoreline-object and corner-object identifiers from canonical serialized model content
  - `1X2` pricing from explicit score comparisons
  - `BTTS` pricing from the same frozen scoreline matrix
  - one generic settlement engine for integer, half, and quarter lines
  - settlement-aware fair-price arithmetic with pushes, half-wins, and half-losses
  - Asian handicap pricing under a fixed `home_handicap` line convention
  - corner-total pricing from the Day 8 corner object only
  - a fixed engineering verification line catalog for goals totals, Asian handicap, and corner totals
- hardened Day 9 documentation semantics while touching the boundary:
  - time decay is now stated explicitly in `days`
  - `xi=0.001` is documented as a per-day decay rate rather than compared unitlessly to half-week references
- generated the Day 10 artifacts:
  - `research/football_research/football_day10_market_pricing_2026-08-18_manifest.json`
  - `research/football_research/football_day10_market_pricing_2026-08-18_pricing_inputs.jsonl`
  - `research/football_research/football_day10_market_pricing_2026-08-18_market_prices.jsonl`
  - `research/football_research/football_day10_market_pricing_2026-08-18.md`
- generated the Day 10 acceptance audit:
  - `research/spec_migration/FOOTBALL_DAY10_ACCEPTANCE_AUDIT_2026-08-18.md`
- verified the Day 10 real-archive result:
  - sanitized pricing inputs: `9664`
  - market-price rows: `473536`
  - market-family rows:
    - `1x2`: `28992`
    - `btts`: `19328`
    - `goals_total`: `115968`
    - `asian_handicap`: `173952`
    - `corner_total`: `135296`
  - rows carrying scoreline source ids: `338240`
  - rows carrying corner source ids: `135296`
  - forbidden realized-outcome / bookmaker / edge / stake fields in output rows: `0`
- verified deterministic replay for the Day 10 layer:
  - repeated fresh runs matched for sanitized pricing inputs
  - repeated fresh runs matched for all market-price rows
  - repeated fresh runs matched for all non-timestamp manifest fields

## Active migration workstream

- keep the August 2026 PDF-first authority order as the implementation guardrail
- keep football development centered on GAP and downstream model contracts
- treat Day 7 as closed for the currently loaded football scope
- treat Day 8 as closed for the currently loaded football scope
- treat Day 9 as closed for the currently loaded football scope
- treat Day 10 as closed for the currently loaded football scope
- begin Day 11 only after preserving the Day 10 fair-price contract:
  - do not collapse push or quarter-line markets into a fake binary probability
  - define exactly how Day 11 `model_probability` and edge arithmetic should represent settlement-aware markets

## Baseline / legacy research

These assets remain preserved, but they are not the current development priority:

- football Poisson `1X2` v1-v4
- the older `1X2` decision-layer and threshold experiments
- the current football watchlist/live review flow
- historical close-vs-close football signal studies
- previous Poisson finalization and optimizer diagnostics

This work is still useful for benchmarking, reusable Dixon-Coles arithmetic, and migration
reference. It should not quietly become the default football product direction again.

## Current constraints

- the supplied PDFs and companion Markdown files are not identical, so requirement conflicts must continue to be recorded explicitly
- the current repository/runtime/database are still shared across football and tennis even though the target architecture requires separation
- the currently loaded football research archive covers only `ENG1`, `ENG2`, `ENG3`, `ENG4`, and `ESP1`, not the full football-spec competition scope
- `SP1` referee coverage is incomplete in the current store, which limits later cards/referee-dependent veto work for that league
- no resolved production sharp-reference live feed exists yet
- the historical Day 8 and Day 9 parameters in the loaded archive are valid for walk-forward evaluation, but they are not automatically a current forward-production fit for 2026-27 paper predictions
- no final PostgreSQL deployment design should be assumed yet

## Immediate next actions

1. preserve the Day 7 through Day 10 artifacts as the frozen baseline before Day 11 work
2. define the Day 11 probability / edge semantics for push and quarter-line markets without pretending they are ordinary binary outcomes
3. resolve the forward-production-fit path for Day 8 conversion coefficients, Day 8 corner dispersion / distribution selection, and Day 9 rho before current 2026-27 paper output
4. continue treating the broader repo/database split, archive-scope completion, live-odds stack, and sharp-reference provider resolution as separate unresolved workstreams

## Current bottom line

The repo now has a tested generic GAP core, three deterministic chronological football GAP lanes,
logged walk-forward lambda fitting, a chronological Day 8 expected-goals conversion layer, an
evidence-based corner-distribution comparison, a chronological Day 9 Dixon-Coles scoreline layer,
and a Day 10 pricing layer that derives mathematically correct fair prices from frozen pre-match
model objects without seeing bookmaker prices or realized outcomes.

Day 10 is `COMPLETE` for the currently loaded football scope.

That does not make the broader August 2026 football build “done.” Shots and shots-on-target markets
are still outside the implemented Day 10 market family, the full-spec historical scope is still
incomplete, and actionable paper-candidate work still depends on the unresolved live odds,
sharp-reference, de-vig, locking, forward-fit, and decision-logging infrastructure.

Model readiness has now crossed the fair-price boundary, but paper-candidate readiness is still
materially later. Experimental football suggestions should not be described as ready until the
Day 11 arithmetic/manager layer, live odds capture, sharp reference, de-vig/booksum handling,
best-price selection, prediction locking, and logging rules are all in place.
