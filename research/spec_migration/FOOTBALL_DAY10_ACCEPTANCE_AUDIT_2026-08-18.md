# Football Day 10 Acceptance Audit

Date: Tuesday, August 18, 2026

Authoritative basis:

- `initial_data/00-developer-brief.pdf`
- `initial_data/02-football-build-spec.pdf`
- `initial_data/03-thirty-day-plan.pdf`

Scope of this audit:

- close Day 10 for the currently loaded football archive scope only
- preserve Day 7, Day 8, and Day 9 artifacts as frozen upstream inputs
- stop at fair prices only
- do not authorize Day 11 arithmetic shortcuts that collapse push or quarter-line markets into ordinary binary probabilities

## Fixed Day 10 interface interpretation

- Day 10 consumes a strict sanitized pre-match pricing input
- the Day 9 scoreline matrix is the single canonical goals object
- the Day 8 corner object is the single canonical corners object
- Day 10 does not ingest bookmaker prices, de-vig outputs, edges, thresholds, staking, or recommendations
- every market row must identify exactly which scoreline object or corner object produced it

## Fixed Day 10 pricing contract

- pricing model version: `football_day10_market_pricing_v1`
- settlement engine version: `football_day10_settlement_engine_v1`
- output schema version: `football_day10_market_price_record_v1`
- sanitized input contract: `football_day10_sanitized_prematch_input_v1`
- goals interface policy: `persisted_day9_scoreline_matrix_is_canonical_goals_object_v1`
- corners interface policy: `persisted_day8_corner_distribution_is_canonical_v1`
- settlement pricing policy: `zero_ev_with_partial_and_push_settlement_v1`
- Asian handicap line convention: `home_handicap`
- corner support policy: `adaptive_support_measure_tail_then_renormalize_v1`

Engineering verification line catalog:

- goals totals: `1.5`, `2.0`, `2.25`, `2.5`, `2.75`, `3.0`
- Asian handicap: `-1.0`, `-0.75`, `-0.5`, `-0.25`, `0.0`, `0.25`, `0.5`, `0.75`, `1.0`
- corner totals: `8.5`, `9.0`, `9.25`, `9.5`, `9.75`, `10.0`, `10.5`

## Acceptance table

| Check | Status | Code / artifact evidence | Test evidence |
| --- | --- | --- | --- |
| sanitized pre-match input excludes outcomes and market fields | `PASS` | `code/src/sports_ai/experiments/football_day10_market_pricing.py`; pricing-input artifact contains only pre-match fields | `code/tests/test_football_day10_market_pricing.py::test_day10_sanitized_input_excludes_outcomes_and_market_data` |
| all goals markets consume the persisted Day 9 matrix directly | `PASS` | goals-market rows point to `source_scoreline_object_id`; no Day 10 Poisson rebuild exists | `code/tests/test_football_day10_market_pricing.py::test_day10_1x2_uses_explicit_score_comparisons_and_conserves_mass`; `code/tests/test_football_day10_market_pricing.py::test_day10_asian_handicap_matches_1x2_invariants` |
| `1X2` is derived from explicit score comparisons and conserves mass | `PASS` | Day 10 module sums `i>j`, `i==j`, `i<j` directly | `code/tests/test_football_day10_market_pricing.py::test_day10_1x2_uses_explicit_score_comparisons_and_conserves_mass` |
| `BTTS` is derived from the same matrix and is complementary | `PASS` | Day 10 module prices `BTTS` from `i>=1` and `j>=1` on the frozen matrix | `code/tests/test_football_day10_market_pricing.py::test_day10_btts_is_complementary` |
| generic settlement engine handles integer, half, and quarter totals | `PASS` | one settlement engine prices totals with split-component lines recorded per row | `code/tests/test_football_day10_market_pricing.py::test_day10_integer_goals_total_handles_pushes_correctly`; `code/tests/test_football_day10_market_pricing.py::test_day10_quarter_goals_total_splits_stake_into_adjacent_lines` |
| fair price arithmetic is settlement-aware rather than naive reciprocal win probability | `PASS` | market rows persist full settlement buckets plus effective win/loss stake mass | `code/tests/test_football_day10_market_pricing.py::test_day10_fair_price_formula_reduces_to_binary_and_push_only_cases` |
| Asian handicap uses the same settlement engine and matches strong `1X2` invariants | `PASS` | AH rows share the generic settlement engine and carry `home_handicap` metadata | `code/tests/test_football_day10_market_pricing.py::test_day10_asian_handicap_matches_1x2_invariants`; `code/tests/test_football_day10_market_pricing.py::test_day10_asian_handicap_settlement_examples`; `code/tests/test_football_day10_market_pricing.py::test_day10_asian_handicap_home_away_symmetry_holds_under_team_swap` |
| malformed lines are rejected instead of rounded silently | `PASS` | Day 10 line parser enforces integer / half / quarter steps only | `code/tests/test_football_day10_market_pricing.py::test_day10_invalid_lines_are_rejected` |
| corner totals come from the Day 8 corner object only | `PASS` | corner-market rows point to `source_corner_object_id` and use persisted home/away corner means plus selected family and dispersion | `code/tests/test_football_day10_market_pricing.py::test_day10_corner_distribution_tracks_tail_mass_and_support` |
| `as_of <= kickoff` and model lineage are preserved | `PASS` | sanitized inputs and market rows persist kickoff, `as_of`, model versions, and deterministic object ids | `code/tests/test_football_day10_market_pricing.py::test_day10_rejects_as_of_after_kickoff`; `code/tests/test_football_day10_market_pricing.py::test_day10_model_versions_and_object_ids_persist_in_artifacts` |
| Day 10 does not mutate Day 9 outputs | `PASS` | Day 10 can consume an injected frozen Day 9 result without modifying it | `code/tests/test_football_day10_market_pricing.py::test_day10_does_not_mutate_day9_outputs` |
| deterministic replay passes | `PASS` | repeated fresh runs matched for pricing inputs, market rows, and non-timestamp manifest content | `code/tests/test_football_day10_market_pricing.py::test_day10_is_deterministic_and_repeatable` |
| full suite passes after Day 10 | `PASS` | suite grew from `155 passed` before the Day 10 change set to `175 passed` after | `pytest -q` |

## Real-archive summary

Artifact: `research/football_research/football_day10_market_pricing_2026-08-18_manifest.json`

- sanitized pricing inputs: `9664`
- market-price rows: `473536`
- market-family rows:
  - `1x2`: `28992`
  - `btts`: `19328`
  - `goals_total`: `115968`
  - `asian_handicap`: `173952`
  - `corner_total`: `135296`
- rows with scoreline source ids: `338240`
- rows with corner source ids: `135296`
- forbidden realized-outcome / bookmaker / edge / stake fields found in market rows: `0`

Observed artifact schema properties:

- pricing-input top-level keys are limited to fixture identity, kickoff, `as_of`, `model_versions`, `scoreline_object`, `corner_object`, and contract metadata
- scoreline objects persist expected-goal metadata, rho, xi, the frozen matrix, and truncation / normalization metadata
- corner objects persist predicted home/away/total corners, selected family, dispersion, and deterministic object ids
- market rows persist fixture identity, market family, line, selection, settlement buckets, effective win/loss stake mass, fair price, numerical metadata, model versions, and source object ids

## Interpretation notes

- Day 10 is compliant and deterministic for the currently loaded football scope
- the implementation now satisfies the bounded Day 10 requirement: given frozen pre-match model objects, the system can derive mathematically correct fair prices without seeing a bookmaker price or match outcome
- Day 10 does not complete every football market in the wider specification
- shots and shots-on-target market pricing remain outside the implemented Day 10 market family
- Day 10 also does not resolve current/live forward-production fit for Day 8 and Day 9 parameters

## Day 11 arithmetic note

The most important carry-forward issue is now explicit:

- the developer brief arithmetic written like `EV = p * odds - 1` is only directly valid for binary no-push markets
- Day 10 preserves the correct settlement-aware fair price and settlement buckets
- Day 11 must define how any `model_probability` or edge field represents push and quarter-line markets before thresholds or manager rules are applied

## Day 10 result

All Day 10 acceptance checks pass for the currently loaded football archive scope.

Day 10 status: `COMPLETE`

Day 11 authorization: `YES`, with the explicit requirement that settlement-aware probability semantics remain unresolved until defined rather than guessed

New blockers introduced by Day 10:

- no new blocker outranks the existing archive-scope, sharp-reference, and live-odds stack blockers
- one Day 11 arithmetic ambiguity is now elevated explicitly: push and quarter-line edge semantics
- one live-readiness dependency is now elevated explicitly: as-of-current forward-production fitting for Day 8 and Day 9 football parameters

Earliest credible experimental-paper-output milestone:

- Day 10 closes the fair-price derivation boundary
- credible experimental paper outputs still require Day 11 arithmetic / manager work, live odds, a resolved sharp reference, de-vig/booksum handling, best-price selection, prediction locking before market reveal, decision logging, and current forward-production parameter snapshots
