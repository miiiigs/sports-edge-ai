# Football Day 9 Acceptance Audit

Date: Monday, August 17, 2026

Authoritative basis:

- `initial_data/00-developer-brief.pdf`
- `initial_data/02-football-build-spec.pdf`
- `initial_data/03-thirty-day-plan.pdf`

Scope of this audit:

- close Day 9 for the currently loaded football archive scope only
- preserve Day 7 and Day 8 artifacts as frozen upstream inputs
- do not authorize Day 10 implementation beyond interface planning unless all checks below pass

## Fixed Day 9 interface interpretation

- Day 8 `expected_goals_home` and `expected_goals_away` are the canonical Day 9 Poisson intensities
- Day 9 fits only the downstream Dixon-Coles low-score dependence layer
- Day 9 does not fit a second competing goals model
- explicit Day 9 gamma is intentionally omitted to avoid double-counting the competition-and-venue asymmetry already encoded in Day 8

## Fixed Day 9 model contract

- scoreline model version: `football_day9_dixon_coles_scoreline_v1`
- baseline matrix support: `0..10 x 0..10`
- fixed rho grid: `-0.20`, `-0.15`, `-0.13`, `-0.10`, `-0.05`, `0.00`, `0.05`
- fixed xi grid: `0.0`, `0.001`
- time-decay unit: `days`
- variant count: `210`
- tail policy:
  - measure independent-Poisson tail mass beyond `10`
  - record raw in-matrix mass explicitly
  - renormalize the in-matrix scoreline probabilities to sum to one
  - never discard tail mass silently

## Walk-forward design

- preserve the inherited `2122` warm-up boundary from Day 7 and Day 8
- evaluate `2223`, `2324`, `2425`, and `2526`
- fit rho chronologically on prior Day 8 expected-goal rows and historical score outcomes only
- keep competitions isolated
- default to `rho=0.0`, `xi=0.0` for the first evaluation season in each competition because no prior scoreline evidence exists yet
- choose xi for later predictions by cumulative prior out-of-sample fold log likelihood, preferring `xi=0.0` on ties
- keep odds, de-vig, edge, thresholds, staking, and recommendations outside the Day 9 layer
- carry Day 8 predicted home and away corners forward in the Day 9 historical row contract so Day 10 can reconstruct the frozen corners object without touching realized outcomes

## Acceptance table

| Check | Status | Code / artifact evidence | Test evidence |
| --- | --- | --- | --- |
| Day 8 -> Day 9 interface is explicit and downstream-only | `PASS` | `code/src/sports_ai/experiments/football_day9_scoreline_model.py`; `docs/DECISION_LOG.md` | `code/tests/test_football_day9_scoreline_model.py::test_day9_does_not_mutate_day8_outputs` |
| plain Poisson baseline from Day 8 expected goals exists | `PASS` | Day 9 module builds explicit independent-Poisson scoreline matrices from Day 8 expected goals | `code/tests/test_football_day9_scoreline_model.py::test_day9_rho_zero_collapses_to_plain_poisson` |
| canonical Dixon-Coles tau correction exists | `PASS` | Day 9 module implements the canonical tau adjustment on `0-0`, `1-0`, `0-1`, `1-1` only | `code/tests/test_football_day9_scoreline_model.py::test_day9_tau_touches_only_four_low_score_cells` |
| rho is fitted chronologically and league-isolated | `PASS` | `research/football_research/football_day9_scoreline_model_2026-08-17_rho_variants.json` and fold records log competition-specific prior-season training windows | `code/tests/test_football_day9_scoreline_model.py::test_day9_rho_variant_records_are_chronological`; `code/tests/test_football_day9_scoreline_model.py::test_day9_preserves_league_isolation` |
| rho=0 collapses to plain Poisson | `PASS` | Day 9 matrix builder preserves exact Poisson equivalence at `rho=0` | `code/tests/test_football_day9_scoreline_model.py::test_day9_rho_zero_collapses_to_plain_poisson` |
| draw-probability effect is measured explicitly | `PASS` | Day 9 predictions and fold records persist plain draw probability, Dixon-Coles draw probability, and their difference | `code/tests/test_football_day9_scoreline_model.py::test_day9_draw_correction_is_measured_explicitly` |
| time-decay comparison is implemented and reproducible | `PASS` | manifest time-decay summary and fold-level xi candidate comparisons exist for every loaded competition | `code/tests/test_football_day9_scoreline_model.py::test_day9_time_decay_comparison_is_reproducible` |
| full `0..10 x 0..10` scoreline matrix is persisted | `PASS` | Day 9 prediction rows persist `scoreline_matrix` plus matrix metadata | `code/tests/test_football_day9_scoreline_model.py::test_day9_model_versions_persist_in_artifacts` |
| tail mass and normalization are explicit | `PASS` | Day 9 predictions persist tail mass, raw matrix mass, normalization factor, and normalization error | `code/tests/test_football_day9_scoreline_model.py::test_day9_matrix_mass_policy_is_explicit` |
| market data is absent from Day 9 fitting | `PASS` | Day 9 module and artifacts contain no sportsbook odds, de-vig, edge, or recommendation fields | `code/tests/test_football_day9_scoreline_model.py::test_day9_no_market_odds_are_required` |
| as_of and versions are preserved | `PASS` | Day 9 predictions persist `as_of`, kickoff, GAP model versions, Day 8 conversion model version, and Day 9 scoreline model version | `code/tests/test_football_day9_scoreline_model.py::test_day9_predictions_preserve_as_of_and_kickoff_order`; `code/tests/test_football_day9_scoreline_model.py::test_day9_model_versions_persist_in_artifacts` |
| deterministic replay passes | `PASS` | repeated real-store runs matched for predictions, rho variants, fold records, diagnostics, final rho, and time-decay summaries | `code/tests/test_football_day9_scoreline_model.py::test_day9_is_deterministic`; `code/tests/test_football_day9_scoreline_model.py::test_day9_fitted_rho_is_reproducible` |
| full suite passes after Day 9 | `PASS` | suite grew from `140 passed` before the Day 9 change set to `155 passed` after | `pytest -q` |

## Real-archive summary

Overall diagnostics from `football_day9_scoreline_model_2026-08-17_manifest.json`:

- prediction count: `9664`
- plain-Poisson log likelihood: `-27914.393116660576`
- Dixon-Coles log likelihood: `-27917.242266083787`
- log-likelihood improvement: `-2.849149423211202`
- average plain draw probability: `0.25253984695834336`
- average Dixon-Coles draw probability: `0.2537535169679147`
- average draw-probability difference: `0.001213670009571341`
- draw-probability increase fixture share: `0.17135761589403972`
- average tail mass beyond `10`: `2.185330003864616e-06`
- max tail mass beyond `10`: `0.00022393919680585572`
- max matrix normalization error: `2.220446049250313e-16`
- observed scorelines outside matrix support: `0`

Final selected rho by competition:

- `ENG1`: `0.00`
- `ENG2`: `0.00`
- `ENG3`: `0.00`
- `ENG4`: `-0.05`
- `ESP1`: `0.00`

Fold-level fitted rho used for predictions:

- `ENG1`: `2223=0.00`, `2324=0.05`, `2425=0.00`, `2526=0.00`
- `ENG2`: `2223=0.00`, `2324=0.00`, `2425=0.00`, `2526=0.00`
- `ENG3`: `2223=0.00`, `2324=0.00`, `2425=0.00`, `2526=0.00`
- `ENG4`: `2223=0.00`, `2324=-0.05`, `2425=-0.05`, `2526=-0.05`
- `ESP1`: `2223=0.00`, `2324=0.05`, `2425=0.00`, `2526=0.00`

Time-decay comparison:

- `ENG1`: `xi=0.0` and `xi=0.001` per day tied at cumulative out-of-sample log likelihood `-3414.9721247855177`, so `xi=0.0` stayed selected
- `ENG2`: tie at `-4709.696288654277`, so `xi=0.0` stayed selected
- `ENG3`: tie at `-4761.077159485858`, so `xi=0.0` stayed selected
- `ENG4`: tie at `-4901.22732885671`, so `xi=0.0` stayed selected
- `ESP1`: tie at `-3235.9768540415944`, so `xi=0.0` stayed selected

## Interpretation notes

- the Day 9 implementation is compliant and deterministic for the currently loaded football scope
- the held-out evidence in this scope does not show an overall likelihood improvement from the fitted Dixon-Coles correction relative to plain Poisson
- the draw-probability effect is positive on average overall, but it does not generally rise across every competition or every fixture
- the time-decay comparison uses fixture age measured in days, so `xi=0.001` here is a per-day decay rate and should not be compared to half-week references without converting units
- those findings are recorded as evidence rather than being forced toward the PDF sanity references

## Deterministic replay result

Repeated fresh Day 9 runs on the loaded research store matched exactly for:

- predictions
- rho-variant records
- fold records
- diagnostics
- final rho summaries
- time-decay summaries

## Day 9 result

All Day 9 acceptance checks pass for the currently loaded football archive scope.

Day 9 status: `COMPLETE`

Day 10 authorization: `YES`

New blockers introduced by Day 9: `NONE`

Important limitation:

- this does not remove the broader blockers around incomplete football-spec archive scope, unresolved sharp-reference and live-odds provider work, shared football/tennis runtime architecture, or downstream Day 10 through Day 11 market and manager implementation work

Earliest credible experimental-paper-output milestone:

- Day 9 makes the model layer one major milestone away from fair-price generation
- credible experimental paper outputs still require Day 10 market derivation, live odds, a resolved sharp reference, de-vig/booksum handling, best-price selection, prediction locking before market reveal, and decision logging
