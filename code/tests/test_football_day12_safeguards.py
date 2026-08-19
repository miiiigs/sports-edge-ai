import csv
import json
from copy import deepcopy

import pytest

from sports_ai.experiments.football_day12_product_outputs import (
    render_day12_acceptance_audit,
    write_day12_product_outputs,
)
from sports_ai.experiments.football_day12_safeguards import (
    DAY12_BLOCKED_MISSING_XG_DATA,
    DAY12_DERIVATION_ERROR,
    DAY12_EXPERIMENTAL_CANDIDATE,
    DAY12_REJECTED_BY_AH_RULE,
    DAY12_REJECTED_BY_MULTIPLE_VETOES,
    DAY12_REJECTED_BY_XG_VETO,
    DAY12_REVIEW_REQUIRED,
    Day12Config,
    load_day11_preliminary_candidates,
    run_day12_safeguard_experiment,
)


def _day11_candidate(
    *,
    decision_id,
    fixture_id="evt_001",
    competition_id="ENG3",
    competition_name="League One",
    selection="HOME",
    market_family="1x2",
    line=None,
    decision="PRELIMINARY_CANDIDATE",
    edge=0.12,
):
    return {
        "available_offer_count": 1,
        "best_available_bookmaker": "MarketMax",
        "best_available_market_snapshot_id": f"snap_{decision_id}",
        "best_available_offer_count": 1,
        "best_available_price": 2.25,
        "best_available_provider_market_id": f"provider_{decision_id}",
        "best_available_source": "max_closing_1x2",
        "competition_id": competition_id,
        "competition_name": competition_name,
        "competition_threshold": 0.06,
        "day11_manager_core_ready": "YES",
        "decision": decision,
        "decision_as_of": "2026-08-18T15:00:00",
        "decision_id": decision_id,
        "decision_record_schema_version": "football_day11_decision_record_v1",
        "devigged_reference_probability": 0.40,
        "division_code": "E2",
        "effective_loss_stake_probability": 0.60,
        "effective_win_stake_probability": 0.40,
        "event_id": fixture_id,
        "final_devig_configuration_version": "football_day11_reference_devig_v1",
        "final_devig_converged": True,
        "final_devig_iterations": 20,
        "final_devig_method": "shin",
        "final_devig_parameter_name": "z",
        "final_devig_parameter_value": 0.01,
        "fixture_id": fixture_id,
        "full_loss_probability": 0.60,
        "full_win_probability": 0.40,
        "half_loss_probability": 0.0,
        "half_win_probability": 0.0,
        "input_label": "HISTORICAL / DEMONSTRATION MARKET INPUT",
        "kickoff_at": "2026-08-18T15:00:00",
        "line": line,
        "live_paper_pipeline_ready": "NO",
        "manager_policy_version": "football_day11_manager_policy_v1",
        "manager_version": "football_day11_manager_core_v1",
        "market_as_of": "2026-08-18T15:00:00",
        "market_family": market_family,
        "match_date": "2026-08-18",
        "minimum_acceptable_price": 2.10,
        "model_as_of": "2026-08-18T15:00:00",
        "model_fair_decimal_price": 2.50,
        "model_versions": {
            "conversion_model_version": "football_day8_goal_conversion_v1",
            "corner_comparison_version": "football_day8_corner_distribution_comparison_v1",
            "day7_gap_baseline_experiment_version": "football_gap_lambda_fitting_v1",
            "day8_experiment_version": "football_day8_goal_conversion_v1",
            "day9_experiment_version": "football_day9_scoreline_model_v1",
            "gap_model_version_corners": "football_gap_corners_fitted_v1",
            "gap_model_version_shots": "football_gap_shots_fitted_v1",
            "gap_model_version_shots_on_target": "football_gap_shots_on_target_fitted_v1",
            "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
        },
        "pricing_model_version": "football_day10_market_pricing_v1",
        "probability_gap_vs_reference": 0.03,
        "push_probability": 0.0,
        "reason_codes": ["PASS_EDGE_THRESHOLD", "PASS_MINIMUM_PRICE"],
        "reference_bookmaker": "Pinnacle",
        "reference_booksum": 1.03,
        "reference_market_snapshot_id": f"ref_{decision_id}",
        "reference_raw_implied_probability": 0.42,
        "reference_source": "pinnacle_closing_1x2",
        "season_code": "2526",
        "selection": selection,
        "settlement_aware_edge": edge,
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
        "source_corner_object_id": None,
        "source_scoreline_object_id": f"scoreline_{fixture_id}",
    }


def _append_day10_row(
    rows,
    *,
    fixture_id,
    competition_id,
    competition_name,
    division_code,
    market_family,
    selection,
    line,
    fair_price,
    full_win,
    push,
    full_loss,
):
    rows.append(
        {
            "fixture_id": fixture_id,
            "event_id": fixture_id,
            "home_team": "Alpha",
            "away_team": "Beta",
            "competition_id": competition_id,
            "competition_name": competition_name,
            "season_code": "2526",
            "division_code": division_code,
            "match_date": "2026-08-18",
            "kickoff_time": "15:00",
            "kickoff_at": "2026-08-18T15:00:00",
            "as_of": "2026-08-18T15:00:00",
            "market_family": market_family,
            "line": "" if line is None else str(line),
            "selection": selection,
            "fair_decimal_price": fair_price,
            "full_win_probability": full_win,
            "half_win_probability": 0.0,
            "push_probability": push,
            "half_loss_probability": 0.0,
            "full_loss_probability": full_loss,
            "expected_goals_home": 1.30,
            "expected_goals_away": 0.95,
            "expected_total_goals": 2.25,
            "predicted_home_corners": 5.1,
            "predicted_away_corners": 4.2,
            "predicted_total_corners": 9.3,
            "scoreline_object_id": f"scoreline_{fixture_id}",
            "corner_object_id": f"corner_{fixture_id}",
            "pricing_model_version": "football_day10_market_pricing_v1",
            "settlement_engine_version": "football_day10_settlement_engine_v1",
            "line_catalog_version": "football_day10_engineering_verification_lines_v1",
        }
    )


def _day10_rows_for_fixture(
    fixture_id="evt_001",
    *,
    competition_id="ENG3",
    competition_name="League One",
    division_code="E2",
    consistent=True,
):
    rows = []
    common = {
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": division_code,
    }
    _append_day10_row(rows, **common, market_family="1x2", selection="HOME", line=None, fair_price=2.50, full_win=0.40, push=0.0, full_loss=0.60)
    _append_day10_row(rows, **common, market_family="1x2", selection="DRAW", line=None, fair_price=3.33, full_win=0.30, push=0.0, full_loss=0.70)
    _append_day10_row(rows, **common, market_family="1x2", selection="AWAY", line=None, fair_price=3.33, full_win=0.30, push=0.0, full_loss=0.70)
    home_minus_half_prob = 0.40 if consistent else 0.35
    _append_day10_row(rows, **common, market_family="asian_handicap", selection="HOME", line=-0.5, fair_price=2.50 if consistent else 2.80, full_win=home_minus_half_prob, push=0.0, full_loss=1.0 - home_minus_half_prob)
    _append_day10_row(rows, **common, market_family="asian_handicap", selection="AWAY", line=0.5, fair_price=3.33, full_win=0.30, push=0.0, full_loss=0.70)
    _append_day10_row(rows, **common, market_family="asian_handicap", selection="HOME", line=0.0, fair_price=2.75, full_win=0.40, push=0.30, full_loss=0.30)
    _append_day10_row(rows, **common, market_family="asian_handicap", selection="HOME", line=-0.75, fair_price=2.80, full_win=0.37, push=0.0, full_loss=0.63)
    _append_day10_row(rows, **common, market_family="asian_handicap", selection="HOME", line=-0.25, fair_price=2.30, full_win=0.44, push=0.0, full_loss=0.56)
    return rows


def _write_day11_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _write_day10_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_xg_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _xg_rows_for_fixture(
    fixture_id="evt_eng1",
    *,
    home_xg=1.20,
    away_xg=0.90,
    as_of="2026-08-18T12:00:00",
    window_end_at="2026-08-18T12:00:00",
    matches_in_window=6,
):
    return [
        {
            "fixture_id": fixture_id,
            "competition_id": "ENG1",
            "season_code": "2526",
            "team_role": "HOME",
            "source": "understat_snapshot",
            "as_of": as_of,
            "window_start_at": "2026-07-20T00:00:00",
            "window_end_at": window_end_at,
            "matches_in_window": matches_in_window,
            "recent_xg_for_per_match": home_xg,
        },
        {
            "fixture_id": fixture_id,
            "competition_id": "ENG1",
            "season_code": "2526",
            "team_role": "AWAY",
            "source": "understat_snapshot",
            "as_of": as_of,
            "window_start_at": "2026-07-20T00:00:00",
            "window_end_at": window_end_at,
            "matches_in_window": matches_in_window,
            "recent_xg_for_per_match": away_xg,
        },
    ]


def _config(tmp_path, *, xg_data_path=None):
    return Day12Config(
        day11_decisions_path=tmp_path / "day11.jsonl",
        day10_model_prices_csv_path=tmp_path / "day10.csv",
        technical_output_dir=tmp_path / "technical",
        product_output_dir=tmp_path / "product/day12",
        latest_output_dir=tmp_path / "product/latest",
        xg_data_path=xg_data_path,
    )


def test_load_day11_preliminary_candidates_filters_non_candidates(tmp_path):
    rows = [
        _day11_candidate(decision_id="cand_1"),
        _day11_candidate(decision_id="no_bet_1", decision="NO_BET"),
    ]
    path = tmp_path / "day11.jsonl"
    _write_day11_jsonl(path, rows)

    selected = load_day11_preliminary_candidates(path)

    assert [row["decision_id"] for row in selected] == ["cand_1"]


def test_day12_preserves_day11_inputs_and_does_not_promote_no_bets(tmp_path):
    rows = [
        _day11_candidate(decision_id="cand_1"),
        _day11_candidate(decision_id="no_bet_1", decision="NO_BET"),
    ]
    original_snapshot = json.dumps(rows, sort_keys=True)
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    _write_day10_csv(tmp_path / "day10.csv", _day10_rows_for_fixture())
    result = run_day12_safeguard_experiment(_config(tmp_path))

    assert json.dumps(rows, sort_keys=True) == original_snapshot
    assert [row["day11_decision_id"] for row in result["candidate_records"]] == ["cand_1"]


def test_day12_ah_selection_is_deterministic_and_rejects_non_canonical_lines(tmp_path):
    rows = [
        _day11_candidate(decision_id="ah_half", market_family="asian_handicap", line=-0.5, edge=0.30),
        _day11_candidate(decision_id="ah_three_quarter", market_family="asian_handicap", line=-0.75, edge=0.10),
        _day11_candidate(decision_id="ah_quarter", market_family="asian_handicap", line=-0.25, edge=0.20),
        _day11_candidate(decision_id="plain_1x2", market_family="1x2"),
    ]
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    _write_day10_csv(tmp_path / "day10.csv", _day10_rows_for_fixture())
    config = _config(tmp_path)

    first = run_day12_safeguard_experiment(config)
    second = run_day12_safeguard_experiment(config)

    by_id = {row["day11_decision_id"]: row for row in first["candidate_records"]}
    assert by_id["ah_three_quarter"]["ah_rule_status"] == "AH_PASS"
    assert by_id["ah_three_quarter"]["final_disposition"] == DAY12_EXPERIMENTAL_CANDIDATE
    assert by_id["ah_half"]["final_disposition"] == DAY12_REJECTED_BY_AH_RULE
    assert by_id["ah_quarter"]["final_disposition"] == DAY12_REJECTED_BY_AH_RULE
    assert by_id["plain_1x2"]["ah_rule_status"] == "AH_NOT_APPLICABLE"
    assert [row["day12_candidate_id"] for row in first["candidate_records"]] == [
        row["day12_candidate_id"] for row in second["candidate_records"]
    ]


def test_day12_derivation_inconsistency_becomes_derivation_error(tmp_path):
    rows = [_day11_candidate(decision_id="cand_1")]
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    _write_day10_csv(tmp_path / "day10.csv", _day10_rows_for_fixture(consistent=False))

    result = run_day12_safeguard_experiment(_config(tmp_path))

    assert result["candidate_records"][0]["final_disposition"] == DAY12_DERIVATION_ERROR


def test_day12_xg_pass_veto_missing_and_future_data_paths(tmp_path):
    eng1_candidate = _day11_candidate(
        decision_id="eng1_cand",
        fixture_id="evt_eng1",
        competition_id="ENG1",
        competition_name="Premier League",
    )
    _write_day11_jsonl(tmp_path / "day11.jsonl", [eng1_candidate])
    _write_day10_csv(
        tmp_path / "day10.csv",
        _day10_rows_for_fixture(
            "evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
            division_code="E0",
        ),
    )

    pass_path = tmp_path / "xg_pass.jsonl"
    _write_xg_jsonl(pass_path, _xg_rows_for_fixture("evt_eng1", home_xg=1.25, away_xg=0.92))
    pass_result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=pass_path))
    assert pass_result["candidate_records"][0]["final_disposition"] == DAY12_EXPERIMENTAL_CANDIDATE

    veto_path = tmp_path / "xg_veto.jsonl"
    _write_xg_jsonl(veto_path, _xg_rows_for_fixture("evt_eng1", home_xg=0.20, away_xg=0.10))
    veto_result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=veto_path))
    assert veto_result["candidate_records"][0]["final_disposition"] == DAY12_REJECTED_BY_XG_VETO

    missing_path = tmp_path / "xg_missing.jsonl"
    _write_xg_jsonl(missing_path, [])
    missing_result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=missing_path))
    assert missing_result["candidate_records"][0]["final_disposition"] == DAY12_BLOCKED_MISSING_XG_DATA

    future_path = tmp_path / "xg_future.jsonl"
    _write_xg_jsonl(
        future_path,
        _xg_rows_for_fixture(
            "evt_eng1",
            as_of="2026-08-18T16:00:00",
            window_end_at="2026-08-18T16:00:00",
        ),
    )
    future_result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=future_path))
    assert future_result["candidate_records"][0]["final_disposition"] == DAY12_REVIEW_REQUIRED


def test_day12_multiple_vetoes_are_collapsed_into_one_final_disposition(tmp_path):
    rows = [
        _day11_candidate(
            decision_id="ah_selected",
            fixture_id="evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
            market_family="asian_handicap",
            line=-0.75,
        ),
        _day11_candidate(
            decision_id="ah_rejected",
            fixture_id="evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
            market_family="asian_handicap",
            line=-0.25,
        ),
    ]
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    _write_day10_csv(
        tmp_path / "day10.csv",
        _day10_rows_for_fixture(
            "evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
            division_code="E0",
        ),
    )
    xg_path = tmp_path / "xg.jsonl"
    _write_xg_jsonl(xg_path, _xg_rows_for_fixture("evt_eng1", home_xg=0.10, away_xg=0.10))

    result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=xg_path))

    by_id = {row["day11_decision_id"]: row for row in result["candidate_records"]}
    assert by_id["ah_selected"]["final_disposition"] == DAY12_REJECTED_BY_XG_VETO
    assert by_id["ah_rejected"]["final_disposition"] == DAY12_REJECTED_BY_MULTIPLE_VETOES


def test_day12_reconciles_every_preliminary_candidate_and_is_repeatable(tmp_path):
    rows = [
        _day11_candidate(decision_id="cand_1", fixture_id="evt_eng3a"),
        _day11_candidate(decision_id="cand_2", fixture_id="evt_eng3b", selection="DRAW", edge=0.35),
        _day11_candidate(
            decision_id="cand_3",
            fixture_id="evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
        ),
    ]
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    day10_rows = (
        _day10_rows_for_fixture("evt_eng3a")
        + _day10_rows_for_fixture("evt_eng3b")
        + _day10_rows_for_fixture(
            "evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
            division_code="E0",
        )
    )
    _write_day10_csv(tmp_path / "day10.csv", day10_rows)
    xg_path = tmp_path / "xg.jsonl"
    _write_xg_jsonl(xg_path, _xg_rows_for_fixture("evt_eng1", home_xg=1.22, away_xg=0.94))
    config = _config(tmp_path, xg_data_path=xg_path)

    first = run_day12_safeguard_experiment(config)
    second = run_day12_safeguard_experiment(config)

    assert len(first["candidate_records"]) == 3
    assert first["candidate_records"] == second["candidate_records"]
    first_manifest = deepcopy(first["manifest"])
    second_manifest = deepcopy(second["manifest"])
    first_manifest.pop("run_timestamp_utc")
    second_manifest.pop("run_timestamp_utc")
    assert first_manifest == second_manifest


def test_day12_product_outputs_and_acceptance_audit_are_generated(tmp_path):
    rows = [
        _day11_candidate(decision_id="cand_1"),
        _day11_candidate(
            decision_id="cand_2",
            fixture_id="evt_eng1",
            competition_id="ENG1",
            competition_name="Premier League",
        ),
    ]
    _write_day11_jsonl(tmp_path / "day11.jsonl", rows)
    day10_rows = _day10_rows_for_fixture("evt_001") + _day10_rows_for_fixture(
        "evt_eng1",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
    )
    _write_day10_csv(tmp_path / "day10.csv", day10_rows)
    xg_path = tmp_path / "xg.jsonl"
    _write_xg_jsonl(xg_path, _xg_rows_for_fixture("evt_eng1", home_xg=1.18, away_xg=0.96))
    result = run_day12_safeguard_experiment(_config(tmp_path, xg_data_path=xg_path))

    paths = write_day12_product_outputs(
        manifest=result["manifest"],
        candidate_records=result["candidate_records"],
        output_dir=tmp_path / "product/day12",
        latest_output_dir=tmp_path / "product/latest",
    )
    audit = render_day12_acceptance_audit(manifest=result["manifest"], product_paths=paths)
    audit_path = tmp_path / "audit.md"
    audit_path.write_text(audit, encoding="utf-8")

    assert "HISTORICAL / DEMONSTRATION MARKET INPUT" in (tmp_path / "product/day12/FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md").read_text(encoding="utf-8")
    assert "NOT REAL-MONEY RECOMMENDATIONS" in (tmp_path / "product/day12/FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md").read_text(encoding="utf-8")
    assert (tmp_path / "product/day12/football_day12_candidates.csv").exists()
    assert (tmp_path / "product/day12/football_day12_candidates.jsonl").exists()
    assert (tmp_path / "product/day12/football_day12_product_manifest.json").exists()
    assert (tmp_path / "product/latest/FOOTBALL_EXPERIMENTAL_CANDIDATES.md").exists()
    assert (tmp_path / "product/latest/football_experimental_candidates.csv").exists()
    assert (tmp_path / "product/latest/football_experimental_candidates_sample.csv").exists()
    assert (tmp_path / "product/latest/FOOTBALL_CURRENT_STATUS.md").exists()
    assert "Football Day 12 Acceptance Audit" in audit_path.read_text(encoding="utf-8")
