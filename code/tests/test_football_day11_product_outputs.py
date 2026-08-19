from sports_ai.experiments.football_day11_product_outputs import write_day11_product_outputs


def _decision_row(decision, edge):
    return {
        "decision_id": f"dec_{decision}_{edge}",
        "fixture_id": "evt_001",
        "event_id": "evt_001",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "season_code": "2526",
        "division_code": "E0",
        "match_date": "2026-08-18",
        "kickoff_at": "2026-08-18T15:00:00",
        "model_as_of": "2026-08-18T15:00:00",
        "decision_as_of": "2026-08-18T15:00:00",
        "market_as_of": "2026-08-18T15:00:00",
        "market_family": "1x2",
        "line": None,
        "selection": "HOME",
        "model_fair_decimal_price": 2.5,
        "source_scoreline_object_id": "scoreline_obj",
        "source_corner_object_id": None,
        "model_versions": {"scoreline_model_version": "football_day9_dixon_coles_scoreline_v1"},
        "pricing_model_version": "football_day10_market_pricing_v1",
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
        "settlement_probabilities": {
            "full_win_probability": 0.4,
            "half_win_probability": 0.0,
            "push_probability": 0.0,
            "half_loss_probability": 0.0,
            "full_loss_probability": 0.6,
        },
        "effective_win_stake_probability": 0.4,
        "effective_loss_stake_probability": 0.6,
        "reference_market_snapshot_id": "ref_1",
        "reference_source": "pinnacle_closing_1x2",
        "reference_bookmaker": "Pinnacle",
        "reference_booksum": 1.06,
        "reference_raw_implied_probabilities": {"HOME": 0.5, "DRAW": 0.28, "AWAY": 0.28},
        "final_devig_method": "shin",
        "final_devig_configuration_version": "football_day11_reference_devig_v1",
        "final_devig_parameter_name": "z",
        "final_devig_parameter_value": 0.03,
        "final_devig_converged": True,
        "final_devig_iterations": 24,
        "devigged_reference_probability": 0.47,
        "power_devig_probability": 0.46,
        "shin_devig_probability": 0.47,
        "probability_gap_vs_reference": -0.07,
        "best_available_price": 2.7,
        "best_available_bookmaker": "Bet365",
        "best_available_source": "bet365_closing_1x2",
        "best_available_market_snapshot_id": "snap_1",
        "best_available_provider_market_id": "provider_1",
        "best_available_offer_count": 2,
        "available_offer_count": 2,
        "competition_threshold": 0.05,
        "minimum_acceptable_price": 2.625,
        "settlement_aware_edge": edge,
        "decision": decision,
        "reason_codes": ["PASS_EDGE_THRESHOLD"],
        "input_label": "HISTORICAL / DEMONSTRATION MARKET INPUT",
        "day11_manager_core_ready": "YES",
        "live_paper_pipeline_ready": "NO",
    }


def test_write_day11_product_outputs_generates_required_files(tmp_path):
    manifest = {
        "manager_version": "football_day11_manager_core_v1",
        "final_devig_method": "shin",
        "threshold_config_version": "football_day11_competition_thresholds_v1",
        "decision_record_count": 2,
        "decision_counts": {
            "PRELIMINARY_CANDIDATE": 1,
            "NO_BET": 1,
            "REJECTED_RULE": 0,
            "MISSING_REQUIRED_MARKET_DATA": 0,
            "STALE_MARKET": 0,
        },
        "day11_manager_core_ready": "YES",
        "live_paper_pipeline_ready": "NO",
    }
    decision_records = [
        _decision_row("PRELIMINARY_CANDIDATE", 0.08),
        _decision_row("NO_BET", 0.01),
    ]

    paths = write_day11_product_outputs(
        manifest=manifest,
        decision_records=decision_records,
        output_dir=tmp_path / "day11",
        latest_output_dir=tmp_path / "latest",
    )

    for key, value in paths.items():
        assert value
        assert tmp_path in __import__("pathlib").Path(value).parents or __import__("pathlib").Path(value).parent == tmp_path

    report = (tmp_path / "day11" / "FOOTBALL_DAY11_MANAGER_REPORT.md").read_text(encoding="utf-8")
    assert "HISTORICAL / DEMONSTRATION MARKET INPUT" in report
    assert "NOT LIVE BETTING OUTPUT" in report
    assert "PRELIMINARY DAY 11 MANAGER DECISIONS" in report
    assert "preliminary candidates" in report.lower()

    status = (tmp_path / "latest" / "FOOTBALL_CURRENT_STATUS.md").read_text(encoding="utf-8")
    assert "DAY11_MANAGER_CORE_READY" in status
    assert "LIVE_PAPER_PIPELINE_READY" in status

