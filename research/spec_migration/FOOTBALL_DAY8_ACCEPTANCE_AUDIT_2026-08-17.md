# Football Day 8 Acceptance Audit

Date: Monday, August 17, 2026

Authoritative basis:

- `initial_data/00-developer-brief.pdf`
- `initial_data/02-football-build-spec.pdf`
- `initial_data/03-thirty-day-plan.pdf`

Scope of this audit:

- close Day 8 for the currently loaded football archive scope only
- preserve Day 7 fitted GAP artifacts as frozen upstream inputs
- do not authorize Day 9 unless all checks below pass

## Stale blocker correction

The earlier blocker that treated the missing `initial_data/00-developer-brief.md` mirror as an
implementation blocker is superseded on Monday, August 17, 2026. The authoritative brief file
`initial_data/00-developer-brief.pdf` is present in the loaded repository and defines `R1-R10`.
The project therefore proceeds under the PDF-first authority order, and the missing Markdown mirror
is now only a documentation-convenience issue.

## Fixed Day 8 model contract

- upstream GAP baseline: the Day 7 fitted `shots`, `shots_on_target`, and `corners` artifacts remain frozen
- conversion model version: `football_day8_goal_conversion_v1`
- conversion form: `league_and_venue_nonnegative_linear_shots_plus_sot_v1`
- goal-conversion equation:
  - `expected_goals_side = beta_shots * predicted_shots_side + beta_shots_on_target * predicted_shots_on_target_side`
- feature discipline:
  - allowed pre-match inputs are predicted `shots`, predicted `shots_on_target`, predicted `corners`, competition, fixture identity, kickoff, `as_of`, model versions, and sample/context metadata
  - historical goals may be used only as supervised downstream outcomes for conversion fitting
  - historical goals may never be used as GAP rating inputs
- corner-family comparison:
  - compare Poisson and negative binomial deterministically
  - fit negative-binomial dispersion from prior training rows only
  - select the family used for each prediction fold from cumulative prior out-of-sample log likelihood only
- variant count:
  - `40` recorded conversion fits across competition, season, and venue

## Walk-forward design

- preserve `2122` as the inherited warm-up season from Day 7
- evaluate `2223`, `2324`, `2425`, and `2526`
- fit each competition/venue conversion only on prior seasons from the same competition
- persist `fixture_id`, competition, kickoff, `as_of`, GAP model version, conversion model version, predicted stats, expected goals, selected corner family, and corner parameters
- prohibit odds, de-vig, edge, stake, scanner, manager, and veto-layer fields from the Day 8 layer

## Acceptance table

| Check | Status | Code / artifact evidence | Test evidence |
| --- | --- | --- | --- |
| no stale developer-brief blocker remains | `PASS` | `docs/BLOCKERS.md`, `docs/REQUIREMENTS_TRACEABILITY.md`, `docs/DECISION_LOG.md` now treat the PDF brief as authoritative and the missing Markdown mirror as superseded | verified against `initial_data/00-developer-brief.pdf` and extracted text |
| league-specific stats-to-goals conversion exists | `PASS` | `code/src/sports_ai/experiments/football_day8_goal_conversion.py`; `research/football_research/football_day8_goal_conversion_2026-08-17_conversion_records.json` | `code/tests/test_football_day8_goal_conversion.py` |
| conversion fitting is chronological and walk-forward only | `PASS` | Day 8 conversion records log per-competition prior-season training windows and prediction-season boundaries | `code/tests/test_football_day8_goal_conversion.py::test_day8_conversion_records_are_chronological` |
| goals never feed back into GAP inputs or state | `PASS` | Day 8 implementation reads frozen Day 7 predictions and football outcomes separately; GAP state remains downstream-immutable | `code/tests/test_football_day8_goal_conversion.py::test_day8_goals_do_not_feed_back_into_gap_predictions`; `code/tests/test_football_day8_goal_conversion.py::test_day8_does_not_modify_gap_state` |
| no market dependency enters Day 8 | `PASS` | Day 8 artifacts and code contain no odds/de-vig/stake/edge outputs | `code/tests/test_football_day8_goal_conversion.py::test_day8_market_odds_are_not_required` |
| league isolation and home/away separation hold | `PASS` | conversion fits are per competition and venue; predictions persist separate home/away expected goals | `code/tests/test_football_day8_goal_conversion.py::test_day8_preserves_league_isolation`; `code/tests/test_football_day8_goal_conversion.py::test_day8_home_and_away_outputs_remain_distinct` |
| persisted outputs carry chronology and versions | `PASS` | Day 8 predictions and manifest persist kickoff, `as_of`, GAP model version, and conversion model version | `code/tests/test_football_day8_goal_conversion.py::test_day8_predictions_preserve_as_of_and_kickoff_order`; `code/tests/test_football_day8_goal_conversion.py::test_day8_generated_artifacts_preserve_model_versions` |
| expected goals remain finite and non-negative | `PASS` | Day 8 prediction rows record non-negative `expected_goals_home` and `expected_goals_away` | `code/tests/test_football_day8_goal_conversion.py::test_day8_expected_goals_are_non_negative_and_finite` |
| Poisson versus NB corner comparison is deterministic and evidence-based | `PASS` | `research/football_research/football_day8_goal_conversion_2026-08-17_corner_records.json` records both families, dispersion, log likelihoods, and selected family per fold | `code/tests/test_football_day8_goal_conversion.py::test_day8_corner_distribution_comparison_is_deterministic`; `code/tests/test_football_day8_goal_conversion.py::test_day8_distribution_selection_matches_recorded_evidence` |
| full repo tests still pass after Day 8 | `PASS` | suite grew from `127 passed` before the Day 8 change set to `140 passed` after | `pytest -q` |

## Expected-goal diagnostics

Overall evaluation diagnostics from the Monday, August 17, 2026 manifest:

- prediction count: `9664`
- mean predicted home goals: `1.5062802507388182`
- mean observed home goals: `1.4605753311258278`
- mean predicted away goals: `1.1408801793856675`
- mean observed away goals: `1.1823261589403973`
- mean predicted total goals: `2.6471604301244858`
- mean observed total goals: `2.642901490066225`
- total-goal MAE: `1.2756111098818814`
- total-goal bias: `0.00425894005826068`

By competition:

- `ENG1`: mean predicted total `2.9724742370368835`, observed `2.9539473684210527`, MAE `1.3296590389452814`, bias `0.018526868615830985`
- `ENG2`: mean predicted total `2.5867065636649094`, observed `2.542119565217391`, MAE `1.2386294824505037`, bias `0.044586998447517984`
- `ENG3`: mean predicted total `2.597709795630786`, observed `2.5905797101449277`, MAE `1.2611663122272938`, bias `0.007130085485858446`
- `ENG4`: mean predicted total `2.535516850925792`, observed `2.599184782608696`, MAE `1.2974388952908007`, bias `-0.06366793168290372`
- `ESP1`: mean predicted total `2.6436743606960027`, observed `2.617763157894737`, MAE `1.2645590995072429`, bias `0.025911202801266037`

By evaluation fold:

- `2223`: mean predicted total `2.604136561862011`, observed `2.5202814569536423`, MAE `1.2801781019709764`, bias `0.0838551049083686`
- `2324`: mean predicted total `2.6500650184202157`, observed `2.8158112582781456`, MAE `1.3209322502426828`, bias `-0.16574623985792997`
- `2425`: mean predicted total `2.6672439552374994`, observed `2.593956953642384`, MAE `1.2760247371213924`, bias `0.07328700159511532`
- `2526`: mean predicted total `2.6671961849782173`, observed `2.6415562913907285`, MAE `1.2253093501924741`, bias `0.025639893587488756`

## Corner distribution evidence

Final selected family by loaded competition scope:

- `ENG1`: `negative_binomial` because cumulative prior out-of-sample log likelihood `-4003.4396761579437` beat Poisson `-4008.173768805129`
- `ENG2`: `negative_binomial` because cumulative prior out-of-sample log likelihood `-5829.997556602502` beat Poisson `-5840.541393174484`
- `ENG3`: `negative_binomial` because cumulative prior out-of-sample log likelihood `-5851.486779588449` beat Poisson `-5867.559220845658`
- `ENG4`: `negative_binomial` because cumulative prior out-of-sample log likelihood `-5752.0922121494505` beat Poisson `-5758.147434907556`
- `ESP1`: `negative_binomial` because cumulative prior out-of-sample log likelihood `-3940.3240150019947` beat Poisson `-3946.27918741787`

Selection behavior by fold:

- every competition defaults to `poisson` on the first evaluation season because no prior out-of-sample evidence exists yet
- later seasons switch only when cumulative earlier out-of-sample evidence favors negative binomial
- the recorded fold table in `football_day8_goal_conversion_2026-08-17_corner_records.json` is deterministic across repeated fresh runs

## Deterministic replay result

Repeated fresh Day 8 runs on the loaded research store matched exactly for:

- predictions
- conversion records
- corner records
- overall diagnostics
- corner-selection summary

## Day 8 result

All Day 8 acceptance checks pass for the currently loaded football archive scope.

Day 8 status: `COMPLETE`

Day 9 authorization: `YES`

New blockers introduced by Day 8: `NONE`

Important limitation:

- this does not remove the broader blockers around incomplete football-spec archive scope, sharp-reference provider resolution, shared football/tennis runtime architecture, unresolved live odds/de-vig/locking stack, or downstream Day 9 through Day 11 implementation work

Day 10-Day 11 confidence:

- still aggressive
- not yet dependable enough to describe as safe
- remaining dependencies still include Day 9 scoreline work, Day 10 market derivation, resolved sharp-reference/live odds inputs, corrected arithmetic and de-vig enforcement, and final decision-lock/logging architecture
